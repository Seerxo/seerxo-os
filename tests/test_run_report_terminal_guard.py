"""POST /tasks/{key}/run-report protects terminal and REVIEW tasks.

Before: report_run wrote `task.status = outcome.task_status` and `task.result` whatever the current state was, so any
trusted (operator/admin) caller moved a DONE or CANCELLED task back to IN_PROGRESS and replaced the founder's result.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cloudflare_access, enums, main, models

ADMIN, HOST, FOUNDER = "a" * 40, "desk.example.invalid", "founder@example.invalid"
T = enums.TaskStatus
REPORT = {"herdr_state": {"state": "completed", "created_at": "2026-10-09T10:00:00Z", "finished_at": "2026-10-09T10:05:00Z"},
          "reported_by": "founder", "learnings": ["a lesson"]}
FINISHED_REPORT = {**REPORT, "herdr_state": {**REPORT["herdr_state"], "state": "finished"}}


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.Agent(key="developer", role="dev", enabled=True))
        for key, st in (("TASK-0001", T.DONE), ("TASK-0002", T.CANCELLED), ("TASK-0003", T.IN_PROGRESS),
                        ("TASK-0004", T.READY), ("TASK-0005", T.REVIEW)):
            s.add(models.Task(key=key, title="t", success_criteria="s", status=st, result="founder-verified"))
        s.add(models.Task(key="TASK-0006", title="dead letter", success_criteria="s",
                          status=T.FAILED, retry_count=3, max_attempts=3, result="failed after max attempts"))
        s.commit()
    for k, v in {"COMPANY_API_TOKEN_ADMIN": ADMIN, "COMPANY_API_TOKEN_DESK": "d" * 40, "COMPANY_API_TOKEN_YOUTUBE": "y" * 40,
                 "API_HOST_IPS": "10.0.6.1", "CF_ACCESS_DESK_HOST": HOST, "API_AUTH_MODE": "enforce"}.items():
        monkeypatch.setenv(k, v)

    async def identity(token, **kw):
        return FOUNDER if token == "founder" else None
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)

    def get_db():
        with Session(engine) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db
    yield engine
    main.app.dependency_overrides.clear()


def _client(host="172.18.0.5", base="http://testserver"):
    return TestClient(main.app, base_url=base, client=(host, 50000))


def _snap(engine, key):
    with Session(engine) as s:
        t = s.scalar(select(models.Task).where(models.Task.key == key))
        return (t.status, t.result, t.retry_count, t.started_at,
                s.scalar(select(func.count()).select_from(models.AgentRun)),
                s.scalar(select(func.count()).select_from(models.AuditLog)),
                s.scalar(select(func.count()).select_from(models.Event)),
                s.scalar(select(func.count()).select_from(models.AgentNote)))


CALLERS = [("anonymous", {}, "172.18.0.5", 401), ("admin", {"authorization": f"Bearer {ADMIN}"}, "172.18.0.5", 403),
           ("local operator", {}, "127.0.0.1", 403), ("host operator", {}, "10.0.6.1", 403)]


@pytest.mark.parametrize("key", ["TASK-0001", "TASK-0002"])
@pytest.mark.parametrize("label,headers,host,code", CALLERS)
@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "blocked"])
def test_nobody_but_the_verified_founder_can_change_a_closed_task(engine, key, label, headers, host, code, state):
    before = _snap(engine, key)
    body = {**REPORT, "herdr_state": {**REPORT["herdr_state"], "state": state, **({"checks": {"status": "failed"}} if state == "failed" else {})}}
    r = _client(host).post(f"/tasks/{key}/run-report", json=body, headers=headers)
    assert r.status_code in (code, 401, 403), (label, r.text)
    assert _snap(engine, key) == before              # no status, result, run, audit, event or note was written


def test_verified_founder_can_report_on_a_closed_task_over_http(engine):
    r = _client(base=f"https://{HOST}").post("/tasks/TASK-0001/run-report", json=REPORT,
                                              headers={"Cf-Access-Jwt-Assertion": "founder"})
    assert r.status_code == 201
    with Session(engine) as s:
        a = s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0001"))
        assert a.actor == f"founder:access:{FOUNDER}"


def test_the_verified_founder_principal_may_report_on_a_closed_task_and_is_the_audit_actor(engine):
    from types import SimpleNamespace
    from app import approval_identity, schemas
    founder = SimpleNamespace(state=SimpleNamespace(principal=approval_identity.founder(FOUNDER)))
    with Session(engine) as db:
        out = main.report_run("TASK-0001", schemas.RunReport(**{**REPORT, "reported_by": "ceo-agent"}), db, founder)
        assert out["task"] == "TASK-0001"
        a = db.scalar(select(models.AuditLog).where(models.AuditLog.action == "task.run-report"))
        assert a.actor == f"founder:access:{FOUNDER}" and a.after["client_claimed_reported_by"] == "ceo-agent"


def test_an_in_process_call_without_a_request_has_no_identity_and_cannot_change_a_closed_task(engine):
    from fastapi import HTTPException
    from app import schemas
    before = _snap(engine, "TASK-0002")
    with Session(engine) as db, pytest.raises(HTTPException) as e:
        main.report_run("TASK-0002", schemas.RunReport(**REPORT), db)
    assert e.value.status_code == 401 and _snap(engine, "TASK-0002") == before


@pytest.mark.parametrize("key", ["TASK-0003", "TASK-0004"])
def test_reports_on_open_tasks_keep_working_and_audit_the_real_caller(engine, key):
    body = {**REPORT, "herdr_state": {**REPORT["herdr_state"], "state": "running"}}
    r = _client("127.0.0.1").post(f"/tasks/{key}/run-report", json=body)
    assert r.status_code == 201, r.text
    with Session(engine) as s:
        a = s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == key, models.AuditLog.action == "task.run-report"))
        assert a.actor == "operator:local" and a.after["client_claimed_reported_by"] == "founder"
        assert s.scalar(select(models.AgentRun).where(models.AgentRun.task_id == a.task_id)) is not None


def test_the_run_report_never_makes_a_task_done(engine):
    r = _client(base=f"https://{HOST}").post("/tasks/TASK-0003/run-report", json=FINISHED_REPORT,
                                              headers={"Cf-Access-Jwt-Assertion": "founder"})
    assert r.status_code == 201 and r.json()["task_status"] != "DONE"


@pytest.mark.parametrize("key", ["TASK-0003", "TASK-0004", "TASK-0005"])
@pytest.mark.parametrize("label,headers,host,code", CALLERS)
def test_operator_cannot_create_or_overwrite_review_via_run_report(engine, key, label, headers, host, code):
    before = _snap(engine, key)
    r = _client(host).post(f"/tasks/{key}/run-report", json=FINISHED_REPORT, headers=headers)
    assert r.status_code == code, (label, r.text)
    assert _snap(engine, key) == before


def test_verified_founder_can_record_review_run(engine):
    r = _client(base=f"https://{HOST}").post("/tasks/TASK-0004/run-report", json=FINISHED_REPORT,
                                              headers={"Cf-Access-Jwt-Assertion": "founder"})
    assert r.status_code == 201
    assert r.json()["task_status"] == "REVIEW"
    with Session(engine) as s:
        a = s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0004"))
        assert a.actor == f"founder:access:{FOUNDER}"


@pytest.mark.parametrize("label,headers,host,code", CALLERS)
def test_operator_cannot_reopen_a_dead_letter_task_with_a_run_report(engine, label, headers, host, code):
    before = _snap(engine, "TASK-0006")
    body = {**REPORT, "herdr_state": {**REPORT["herdr_state"], "state": "running"}}
    r = _client(host).post("/tasks/TASK-0006/run-report", json=body, headers=headers)
    assert r.status_code in (code, 401, 403), (label, r.text)
    assert _snap(engine, "TASK-0006") == before


def test_verified_founder_can_reopen_a_dead_letter_task_with_a_run_report(engine):
    body = {**REPORT, "herdr_state": {**REPORT["herdr_state"], "state": "running"}}
    r = _client(base=f"https://{HOST}").post("/tasks/TASK-0006/run-report", json=body,
                                               headers={"Cf-Access-Jwt-Assertion": "founder"})
    assert r.status_code == 201 and r.json()["task_status"] == "IN_PROGRESS"
    with Session(engine) as s:
        row = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "task.run-report"))
        assert row.actor == f"founder:access:{FOUNDER}"


def test_operator_cannot_overwrite_existing_review_with_a_nonreview_report(engine):
    before = _snap(engine, "TASK-0005")
    r = _client("127.0.0.1").post("/tasks/TASK-0005/run-report", json=REPORT)
    assert r.status_code == 403
    assert _snap(engine, "TASK-0005") == before


def test_company_report_exits_nonzero_when_api_refuses_finished_run(tmp_path):
    state = tmp_path / "tasks" / "TASK-0004" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(FINISHED_REPORT["herdr_state"]))
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/bin/sh\nexit 22\n")  # curl -f returns 22 on an HTTP 403
    ssh.chmod(0o755)
    out = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "scripts/company-report"),
                          "TASK-0004", "--send"], env={**os.environ, "HERDR_PLUGIN_STATE_DIR": str(tmp_path),
                                                       "PATH": f"{tmp_path}:{os.environ['PATH']}"},
                         capture_output=True, text=True, check=False)
    assert out.returncode == 1
    assert "REVIEW requires a verified founder Access session" in out.stderr



def test_a_herdr_report_without_its_run_identity_is_refused_and_records_nothing(engine):
    # Review of #396 (P3-d): without created_at each snapshot added a new STARTED run and the task stuck IN_PROGRESS.
    before = _snap(engine, "TASK-0003")
    body = {**REPORT, "herdr_state": {"state": "running"}}
    for _ in range(2):
        r = _client("127.0.0.1").post("/tasks/TASK-0003/run-report", json=body)
        assert r.status_code == 422 and "created_at" in r.text
    assert _snap(engine, "TASK-0003") == before   # no run, audit, event or note row was written
