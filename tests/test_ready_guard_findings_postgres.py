"""Codex review of #388, findings 4 and 8, on real PostgreSQL.

4: a herdr "running" report followed by its "cancelled" report left the first run row STARTED, so the task stayed
   IN_PROGRESS and every later READY was refused. A report now updates its own herdr run (same created_at); a repeated
   terminal report is idempotent; concurrent reports serialize on the task row; an unrelated open run still blocks.
8: with API_AUTH_MODE=log an anonymous caller could move a task BLOCKED -> READY. The status and run-report routes are
   now always enforced, and READY needs the founder or a host operator in every mode.
"""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import enums, main, models
from tests.test_worker_ownership_postgres import DESK_HOST, FOUNDER_H, KEY, T, _task, engine  # noqa: F401

HERDR = "herdr/project-control-center"
CREATED = "2026-10-20T10:00:00Z"


def _report(state, **extra):
    return TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000)).post(
        f"/tasks/{KEY}/run-report", headers=FOUNDER_H,
        json={"herdr_state": {"state": state, "workflow": {"name": "standard"}, "created_at": CREATED, **extra},
              "reported_by": "herdr", "changed_files": []})


def _facts(engine):
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == KEY))
        runs = db.scalars(select(models.AgentRun).where(models.AgentRun.task_id == t.id).order_by(models.AgentRun.id)).all()
        reports = db.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.entity_key == KEY, models.AuditLog.action == "task.run-report"))
        return t.status, t.retry_count, [(r.provider, r.status) for r in runs], reports


def _set(engine, status):
    with Session(engine) as db:
        db.scalar(select(models.Task).where(models.Task.key == KEY)).status = status
        db.commit()


def test_a_cancelled_report_closes_its_own_running_snapshot_and_requeues(engine):
    _task(engine)
    _set(engine, T.IN_PROGRESS)
    assert _report("running").status_code == 201
    assert _report("cancelled", cancelled_at="2026-10-20T10:05:00Z").status_code == 201
    status, retries, runs, reports = _facts(engine)
    assert status == T.READY                                         # not stuck IN_PROGRESS
    assert runs == [(HERDR, enums.AgentRunStatus.FAILED)]            # one row for one herdr run, closed
    assert retries == 1 and reports == 2                             # history kept: both reports audited


def test_a_repeated_cancelled_report_is_idempotent(engine):
    _task(engine)
    _set(engine, T.IN_PROGRESS)
    _report("running")
    first = _report("cancelled")
    again = _report("cancelled")
    assert first.status_code == again.status_code == 201 and again.json()["ignored"] == "duplicate"
    status, retries, runs, reports = _facts(engine)
    assert status == T.READY and retries == 1 and len(runs) == 1 and reports == 2   # no second retry, row or audit


def test_concurrent_cancelled_reports_serialize_on_the_task_row(engine):
    _task(engine)
    _set(engine, T.IN_PROGRESS)
    _report("running")
    go, codes = threading.Barrier(4), []

    def send():
        go.wait(timeout=10)
        codes.append(_report("cancelled").status_code)
    threads = [threading.Thread(target=send) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    status, retries, runs, reports = _facts(engine)
    assert codes == [201] * 4
    assert status == T.READY and retries == 1 and runs == [(HERDR, enums.AgentRunStatus.FAILED)] and reports == 2


def test_an_unrelated_open_worker_run_still_blocks_the_requeue(engine):
    _task(engine)
    _set(engine, T.IN_PROGRESS)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == KEY))
        db.add(models.AgentRun(task_id=t.id, agent_id=t.assigned_agent_id, provider="claude", status=enums.AgentRunStatus.STARTED))
        db.commit()
    _report("running")
    assert _report("cancelled").status_code == 201
    status, _, runs, _ = _facts(engine)
    assert status == T.IN_PROGRESS                                   # the worker's live run is not doubled
    assert (HERDR, enums.AgentRunStatus.FAILED) in runs and ("claude", enums.AgentRunStatus.STARTED) in runs


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer " + "d" * 40}, {"Cf-Access-Jwt-Assertion": "forged"}])
@pytest.mark.parametrize("route", ["status", "run-report"])
def test_log_mode_never_lets_an_unverified_caller_requeue_a_task(engine, monkeypatch, headers, route):
    """Finding 8: anonymous, the Desk token alone, or a forged assertion, in log mode: refused, nothing written."""
    monkeypatch.setenv("API_AUTH_MODE", "log")
    _task(engine)
    _set(engine, T.BLOCKED)
    before = _facts(engine)
    c = TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000))
    body = ({"status": "READY", "actor": "founder", "reason": "x"} if route == "status"
            else {"herdr_state": {"state": "cancelled", "created_at": CREATED}, "reported_by": "founder", "changed_files": []})
    r = c.post(f"/tasks/{KEY}/{route}", headers=headers, json=body)
    assert r.status_code in (401, 403), r.text
    assert _facts(engine) == before                                  # zero database changes


def test_the_founder_flow_still_works_in_log_mode(engine, monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "log")
    _task(engine)
    _set(engine, T.BLOCKED)
    r = TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000)).post(
        f"/tasks/{KEY}/status", headers=FOUNDER_H, json={"status": "READY", "actor": "anyone", "reason": "Mark READY"})
    assert r.status_code == 200, r.text
    assert _facts(engine)[0] == T.READY


# Independent (Codex) review of the fix itself.
def test_distinct_check_attempts_of_one_run_are_each_recorded(engine):
    """Two failures of the same herdr run with different check attempts are two facts, not a duplicate."""
    _task(engine)
    _set(engine, T.IN_PROGRESS)
    a = _report("verify_failed", checks={"status": "failed", "attempt": 1, "summary": "A"}, updated_at="2026-10-20T10:02:00Z")
    b = _report("verify_failed", checks={"status": "failed", "attempt": 2, "summary": "B"}, updated_at="2026-10-20T10:04:00Z")
    assert a.status_code == b.status_code == 201 and not b.json().get("ignored")
    with Session(engine) as db:
        errors = [r.error for r in db.scalars(select(models.AgentRun).order_by(models.AgentRun.id))]
    assert errors == ["A", "B"] and _facts(engine)[1] == 2


def test_a_delayed_older_snapshot_never_reopens_a_cancelled_run(engine):
    _task(engine)
    _set(engine, T.IN_PROGRESS)
    _report("running", updated_at="2026-10-20T10:00:30Z")
    assert _report("cancelled", updated_at="2026-10-20T10:05:00Z").status_code == 201
    late = _report("implementing", updated_at="2026-10-20T10:01:00Z")   # older, arrives late
    assert late.status_code == 201 and late.json()["ignored"] in ("cancelled", "stale")
    status, retries, runs, _ = _facts(engine)
    assert status == T.READY and retries == 1 and runs == [(HERDR, enums.AgentRunStatus.FAILED)]


def test_duplicate_open_rows_of_one_run_are_all_closed_by_one_cancellation(engine):
    """The parent code could leave several STARTED rows for one herdr run; one cancellation closes them all, once."""
    _task(engine)
    _set(engine, T.IN_PROGRESS)
    from datetime import datetime, timezone
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == KEY))
        for _ in range(2):
            db.add(models.AgentRun(task_id=t.id, provider=HERDR, status=enums.AgentRunStatus.STARTED,
                                   started_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)))
        db.commit()
    assert _report("cancelled", updated_at="2026-10-20T10:05:00Z").status_code == 201
    status, retries, runs, _ = _facts(engine)
    assert status == T.READY and retries == 1 and runs == [(HERDR, enums.AgentRunStatus.FAILED)] * 2


@pytest.mark.parametrize("path, body", [
    ("/tasks", {"title": "x", "success_criteria": "evidence", "status": "READY", "created_by": "founder", "reason": "x"}),
    ("/desk/command", {"text": "do something", "agent": "research", "priority": "P2"}),
    ("/control/agents", {"paused": True, "actor": "founder", "reason": "x"}),
    # P3-a: an event can release WAITING_EXTERNAL work; assignment chooses who runs it.
    ("/events", {"event_type": "EXTERNAL_EVIDENCE", "source": "watchdog", "severity": "INFO", "payload": {}}),
    (f"/tasks/{KEY}/assign", {"agent": "research", "actor": "founder"}),
    # The remaining writes that create or steer work (review of #396, P3).
    ("/webhooks/youtube-auto", {"event": "upload_failed", "detail": "x"}),
    ("/incidents", {"title": "x", "description": "x", "severity": "SEV3", "detected_by": "x"}),
    ("/metrics/observations", {"observations": []}),
    ("/goals", {"title": "x"}),
    ("/backups", {"target": "x", "ok": True}),
])
def test_log_mode_never_lets_an_unverified_caller_create_work_or_flip_the_kill_switch(engine, monkeypatch, path, body):
    monkeypatch.setenv("API_AUTH_MODE", "log")
    _task(engine)
    tables = (models.Task, models.ControlState, models.Incident, models.Goal, models.Event, models.AuditLog)

    def counts():
        with Session(engine) as db:
            return [db.scalar(select(func.count()).select_from(m)) for m in tables]
    before = counts()
    r = TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000)).post(path, json=body)
    assert r.status_code in (401, 403), r.text
    assert counts() == before


def test_log_mode_never_lets_an_unverified_caller_patch_a_goal(engine, monkeypatch):
    # Review of #402: PATCH /goals/{id} is ENFORCE_ALWAYS too (goals steer the reconciler's planning).
    monkeypatch.setenv("API_AUTH_MODE", "log")
    with Session(engine) as db:
        g = models.Goal(title="keep", metric="views", target="10")
        db.add(g)
        db.commit()
        gid = g.id
    with Session(engine) as db:
        before_audit = db.scalar(select(func.count()).select_from(models.AuditLog))
    r = TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000)).patch(
        f"/goals/{gid}", json={"title": "hijacked", "target": "0"})
    assert r.status_code in (401, 403), r.text
    with Session(engine) as db:
        g = db.get(models.Goal, gid)
        assert (g.title, g.target) == ("keep", "10")
        assert db.scalar(select(func.count()).select_from(models.AuditLog)) == before_audit
