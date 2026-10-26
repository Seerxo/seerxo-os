"""LOW-risk autonomous task lifecycle on real PostgreSQL (Issue #190): state machine, not a live model.

Providers are FakeProviders from tests/test_worker.py: this proves claim, evidence, supervision, recovery and the
founder gate against PostgreSQL. It says nothing about what a real Claude/Codex run would answer (UNKNOWN until
independently witnessed). Nothing here publishes, deploys, pays or writes outside the disposable database.
Set LIFECYCLE_TRACE_PATH to also write the run trace of the main scenario as JSON.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import api_auth, cloudflare_access, enums, main, models, registry, worker
from app.providers import Health, ProviderResult
from tests.test_worker import FakeProvider, _good

SPECS = registry.load()
WORKERS = 6
ADMIN, DESK_HOST, FOUNDER = "a" * 40, "desk.example.invalid", "founder@example.invalid"
T = enums.TaskStatus


@pytest.fixture
def engine(monkeypatch):
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=WORKERS + 4)
    models.Base.metadata.drop_all(engine)  # disposable database only (guard above)
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        for key in ("ceo", "developer", "supervisor", "research"):
            db.add(models.Agent(key=key, role=key, enabled=True))
        db.commit()
    for k, v in {"COMPANY_API_TOKEN_ADMIN": ADMIN, "COMPANY_API_TOKEN_DESK": "d" * 40, "COMPANY_API_TOKEN_YOUTUBE": "y" * 40,
                 "API_HOST_IPS": "10.0.6.1", "CF_ACCESS_DESK_HOST": DESK_HOST, "API_AUTH_MODE": "enforce"}.items():
        monkeypatch.setenv(k, v)

    async def identity(token, **kw):
        return FOUNDER if token == "founder-assertion" else None
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)

    def get_db():
        with Session(engine) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db
    try:
        yield engine
    finally:
        main.app.dependency_overrides.clear()
        # The CI command shares this explicitly disposable database with other PostgreSQL suites.
        # Leave no seeded agents behind for their independent fixtures.
        models.Base.metadata.drop_all(engine)
        engine.dispose()


def _task(engine, key="TASK-0100", **over):
    with Session(engine) as db:
        research = db.scalar(select(models.Agent).where(models.Agent.key == "research"))
        t = models.Task(key=key, title="probe", success_criteria="evidence has token", objective="Echo the verification token",
                        status=T.READY, priority=enums.Priority.P3, risk_level=enums.RiskLevel.LOW,
                        assigned_agent_id=research.id,
                        context={"autonomous": True, "scope": "company-os", "verify": {"must_contain": ["selftest-x", key]}})
        for k, v in over.items():
            setattr(t, k, v)
        db.add(t)
        db.commit()
    return key


def _tick(engine, providers):
    with Session(engine) as db:
        return worker.run_once(db, providers, SPECS)


def _state(engine, key):
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == key))
        return t.status, t.retry_count


def _age_runs(engine, hours=1):
    """Make the backoff window pass without sleeping."""
    with Session(engine) as db:
        for run in db.scalars(select(models.AgentRun)):
            if run.ended_at:
                run.ended_at -= timedelta(hours=hours)
        db.commit()


def _api(base=f"https://{DESK_HOST}", host="172.18.0.5"):
    return TestClient(main.app, base_url=base, client=(host, 50000))


FOUNDER_H = {"Cf-Access-Jwt-Assertion": "founder-assertion"}
OPERATOR_H = {"authorization": f"Bearer {ADMIN}"}


def _no_done(engine):
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(models.Task).where(models.Task.status == T.DONE))


def test_low_risk_lifecycle_trace_from_trigger_to_founder_decision(engine):
    with Session(engine) as db:
        key = worker.create_selftest(db)  # TRIGGER -> READY (the same idempotent trigger the worker ships)
        assert worker.create_selftest(db) == key
        assert db.scalar(select(models.Task).where(models.Task.key == key)).status == T.READY
    trace = [{"step": "trigger", "task": key, "status": "READY"}]
    day = datetime.now(timezone.utc).date().isoformat()

    out = _tick(engine, {"claude": FakeProvider("claude", [_good(key, evidence=[f"selftest-{day} and {key}"])])})
    assert out["verdict"] == "APPROVED" and out["task_status"] == "REVIEW" and out["model_calls"] == 1

    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        run = db.scalar(select(models.AgentRun).where(models.AgentRun.task_id == task.id))
        agent = db.get(models.Agent, task.assigned_agent_id)
        audits = db.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == key).order_by(models.AuditLog.id)).all()
        actions = [a.action for a in audits]
        assert actions == ["task.claim", "task.autonomous-run"]
        assert (audits[0].before["status"], audits[0].after["status"]) == ("READY", "IN_PROGRESS")
        assert (audits[1].before["status"], audits[1].after["status"]) == ("IN_PROGRESS", "REVIEW")
        assert run.status == enums.AgentRunStatus.SUCCEEDED and run.supervisor_verdict == enums.SupervisorVerdict.APPROVED
        assert run.ended_at and run.started_at <= run.ended_at and run.result and run.tool_calls["result_hash"]
        assert run.tool_calls["incremental_cost_usd"] == 0 and run.tool_calls["attempts"][0]["provider"] == "claude"
        assert task.status == T.REVIEW and task.artifacts["evidence"] and task.artifacts["run_id"] == run.id
        assert task.completed_at is None and agent.last_success_at is not None  # useful success, recorded
        events = [e.event_type for e in db.scalars(select(models.Event).order_by(models.Event.id))]
        assert events.count("TASK_STARTED") == 1 and events.count("TASK_COMPLETED") == 1
        trace += [{"step": "claim", "audit": actions[0], "from": "READY", "to": "IN_PROGRESS", "at": audits[0].created_at.isoformat() if getattr(audits[0], "created_at", None) else None},
                  {"step": "execute", "run_id": run.id, "agent": agent.key, "provider": run.provider, "fake_provider": True,
                   "started_at": run.started_at.isoformat(), "ended_at": run.ended_at.isoformat(), "result_hash": run.tool_calls["result_hash"]},
                  {"step": "evidence", "evidence": task.artifacts["evidence"], "summary": task.result},
                  {"step": "verify", "supervisor": run.supervisor_verdict.value, "notes": run.supervisor_notes},
                  {"step": "review", "audit": actions[1], "from": "IN_PROGRESS", "to": "REVIEW"}]

    # The worker's REVIEW is not completion: only a verified founder closes it, and only with evidence.
    c = _api()
    before = _no_done(engine)
    for client, headers, code in ((c, {}, 401), (_api("http://testserver"), OPERATOR_H, 403)):
        r = client.post(f"/tasks/{key}/status", json={"status": "DONE", "evidence": "looks fine"}, headers=headers)
        assert r.status_code == code
    assert _state(engine, key)[0] == T.REVIEW and _no_done(engine) == before
    assert c.post(f"/tasks/{key}/status", json={"status": "DONE"}, headers=FOUNDER_H).status_code == 422
    assert _state(engine, key)[0] == T.REVIEW
    r = c.post(f"/tasks/{key}/status", json={"status": "DONE", "evidence": f"verified run {run.id}", "actor": "ceo"}, headers=FOUNDER_H)
    assert r.status_code == 200 and r.json()["status"] == "DONE"
    with Session(engine) as db:
        decision = db.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == key, models.AuditLog.action == "task.status")).one()
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert decision.actor == f"founder:access:{FOUNDER}" and decision.after["client_claimed_actor"] == "ceo"
        assert task.status == T.DONE and task.completed_at is not None
        trace.append({"step": "founder-decision", "actor": decision.actor, "reason": decision.reason, "to": "DONE"})

    # Terminal: the worker never touches it again and a second decision changes nothing material.
    assert _tick(engine, {"claude": FakeProvider("claude", [])})["model_calls"] == 0
    assert _state(engine, key)[0] == T.DONE
    trace.append({"step": "terminal", "status": "DONE", "worker_reclaimed": False})
    if os.environ.get("LIFECYCLE_TRACE_PATH"):
        with open(os.environ["LIFECYCLE_TRACE_PATH"], "w") as f:
            json.dump(trace, f, indent=2, default=str)


def test_concurrent_workers_claim_a_task_exactly_once(engine):
    key = _task(engine)
    gate = Barrier(WORKERS)

    def worker_tick(i):
        fake = FakeProvider("claude", [_good(key)])
        with Session(engine) as db:
            gate.wait()
            out = worker.run_once(db, {"claude": fake}, SPECS)
        return out["model_calls"], fake.calls

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(worker_tick, range(WORKERS)))
    assert sum(c for _, c in results) == 1, results  # exactly one provider call in total
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert task.status == T.REVIEW
        assert db.scalar(select(func.count()).select_from(models.AgentRun)) == 1
        assert db.scalar(select(func.count()).select_from(models.AuditLog).where(models.AuditLog.action == "task.claim")) == 1


def test_simultaneous_first_heartbeats_do_not_crash_a_worker(engine):
    # Break caught (CI on #222): two workers both saw no `worker` control row and the INSERT loser raised
    # UniqueViolation out of run_once. Repeated rounds so the race is actually exercised.
    for _ in range(5):
        with Session(engine) as db:
            db.query(models.ControlState).filter(models.ControlState.key == worker.WORKER_KEY).delete()
            db.commit()
        gate = Barrier(WORKERS)

        def beat(i):
            with Session(engine) as db:
                gate.wait()
                worker._heartbeat(db, tick=i)

        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(beat, range(WORKERS)))          # re-raises any worker's exception
        with Session(engine) as db:
            assert db.get(models.ControlState, worker.WORKER_KEY).value["heartbeat_at"]


def test_raw_claim_race_has_one_winner(engine):
    key = _task(engine)
    gate = Barrier(WORKERS)

    def attempt(_):
        with Session(engine) as db:
            task = db.scalar(select(models.Task).where(models.Task.key == key))
            gate.wait()
            return worker.claim(db, task)

    with ThreadPoolExecutor(WORKERS) as pool:
        assert sorted(pool.map(attempt, range(WORKERS))) == [False] * (WORKERS - 1) + [True]


def test_duplicate_triggers_create_one_open_task(engine):
    gate = Barrier(WORKERS)

    def trigger(_):
        with Session(engine) as db:
            gate.wait()
            return worker.create_selftest(db)

    with ThreadPoolExecutor(WORKERS) as pool:
        keys = set(pool.map(trigger, range(WORKERS)))
    assert len(keys) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(models.Task)) == 1


def test_restart_recovery_requeues_lost_runs_and_unstarted_claims_without_inventing_success(engine):
    key = _task(engine)
    _tick(engine, {"claude": FakeProvider("claude", [{"nonsense": True}])})  # attempt 1 fails -> READY, retry 1
    assert _state(engine, key) == (T.READY, 1)
    old = datetime.now(timezone.utc) - worker.STALE_AFTER - timedelta(minutes=5)
    # A worker claims, starts a run, then dies: run STARTED forever, task IN_PROGRESS.
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert worker.claim(db, task)
        run = models.AgentRun(agent_id=task.assigned_agent_id, task_id=task.id, provider="claude", status=enums.AgentRunStatus.STARTED)
        db.add(run)
        db.commit()
        run.started_at = old
        task.updated_at = old
        db.commit()
    with Session(engine) as db:
        assert worker.recover_stale(db) == [key]
    assert _state(engine, key) == (T.READY, 2)  # the lost attempt is spent, never a success
    with Session(engine) as db:
        lost = db.scalars(select(models.AgentRun).order_by(models.AgentRun.id.desc())).first()
        assert lost.status == enums.AgentRunStatus.FAILED and "worker lost the run" in lost.error
        assert db.scalar(select(func.count()).select_from(models.AgentRun).where(models.AgentRun.status == enums.AgentRunStatus.SUCCEEDED)) == 0
    # Claimed, died before the run existed: requeued without spending a retry.
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert worker.claim(db, task)
        task.updated_at = old
        db.commit()
    with Session(engine) as db:
        assert worker.recover_stale(db) == [key]
    assert _state(engine, key) == (T.READY, 2)
    _age_runs(engine, hours=2)
    assert _tick(engine, {"claude": FakeProvider("claude", [_good(key)])})["task_status"] == "REVIEW"
    # A fresh claim is never stolen by recovery.
    key2 = _task(engine, key="TASK-0101")
    with Session(engine) as db:
        assert worker.claim(db, db.scalar(select(models.Task).where(models.Task.key == key2)))
    with Session(engine) as db:
        assert worker.recover_stale(db) == []
    assert _state(engine, key2)[0] == T.IN_PROGRESS


@pytest.mark.parametrize("error", ["timeout after 600s", "exit 1: provider crashed"])
def test_provider_failures_retry_a_bounded_number_of_times_and_end_failed_never_done(engine, error):
    key = _task(engine)
    fake = FakeProvider("claude", [ProviderResult("claude", Health.AVAILABLE, error=error)] * 5)
    for _ in range(5):
        _tick(engine, {"claude": fake})
        _age_runs(engine, hours=2)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert task.status == T.FAILED and task.retry_count == task.max_attempts == fake.calls
        assert task.completed_at is None and task.result is None
        assert db.scalar(select(func.count()).select_from(models.Event).where(models.Event.event_type == "TASK_FAILED_REPEATEDLY")) == 1
    assert _no_done(engine) == 0 and _tick(engine, {"claude": fake})["model_calls"] == 0  # FAILED is not re-run


def test_quota_exhaustion_cools_down_spends_no_retry_and_never_completes(engine):
    key = _task(engine)
    fake = FakeProvider("claude", [ProviderResult("claude", Health.QUOTA_EXHAUSTED, error="usage limit")])
    out = _tick(engine, {"claude": fake})
    assert out["verdict"] == "NO_CAPACITY" and _state(engine, key) == (T.READY, 0)
    again = _tick(engine, {"claude": fake})  # cooling down: no second provider call
    assert fake.calls == 1 and again["model_calls"] == 0
    assert _no_done(engine) == 0


@pytest.mark.parametrize("output,expected_status,retry", [
    (lambda k: _good(k, evidence=[]), T.READY, 1),                        # missing evidence
    (lambda k: _good(k, evidence=["no token here"]), T.READY, 1),         # evidence lacks required token
    (lambda k: _good("TASK-9999"), T.READY, 1),                           # result for another task
    (lambda k: _good(k, status="failed", error="boom"), T.READY, 1),      # self-reported failure
    (lambda k: _good(k, status="needs_approval", approval_request="may I deploy?"), T.BLOCKED, 0),  # escalation reaches a human
])
def test_supervisor_rejections_never_reach_review_or_done(engine, output, expected_status, retry):
    key = _task(engine)
    _tick(engine, {"claude": FakeProvider("claude", [output(key)])})
    assert _state(engine, key) == (expected_status, retry)
    with Session(engine) as db:
        run = db.scalar(select(models.AgentRun))
        assert run.status != enums.AgentRunStatus.SUCCEEDED and run.supervisor_verdict != enums.SupervisorVerdict.APPROVED
        assert db.scalar(select(models.Agent).where(models.Agent.key == "research")).last_success_at is None
    assert _no_done(engine) == 0


def test_a_heartbeat_is_not_useful_work(engine):
    out = _tick(engine, {"claude": FakeProvider("claude", [])})  # nothing to do
    assert out["model_calls"] == 0
    with Session(engine) as db:
        state = worker.worker_state(db)
        assert state["status"] == "HEALTHY" and state["last"] == "idle" and state["heartbeat_at"]
        assert state["last_run"] is None  # healthy beat, yet not one useful execution
        assert db.scalar(select(func.count()).select_from(models.AgentRun)) == 0
        assert all(a.last_success_at is None for a in db.scalars(select(models.Agent)))
    key = _task(engine)  # a failing run still beats, and still records no success
    _tick(engine, {"claude": FakeProvider("claude", [{"nonsense": True}])})
    with Session(engine) as db:
        assert worker.worker_state(db)["status"] == "HEALTHY" and worker.worker_state(db)["last_run"]["status"] == "FAILED"
        assert all(a.last_success_at is None for a in db.scalars(select(models.Agent)))


def test_paused_agents_claim_nothing(engine):
    from app import control
    key = _task(engine)
    with Session(engine) as db:
        control.set_agents_paused(db, True, actor="founder", reason="test")
    fake = FakeProvider("claude", [_good(key)])
    assert _tick(engine, {"claude": fake})["paused"] is True and fake.calls == 0
    assert _state(engine, key) == (T.READY, 0)


def test_nobody_but_a_verified_founder_can_close_work_and_the_worker_never_does(engine):
    key = _task(engine)
    for base, headers in ((f"https://{DESK_HOST}", {}), ("http://testserver", OPERATOR_H), ("http://testserver", {"authorization": "Bearer " + "d" * 40}),
                          ("http://testserver", {"authorization": "Bearer " + "y" * 40}), (f"https://{DESK_HOST}", {"Cf-Access-Jwt-Assertion": "forged"})):
        r = _api(base).post(f"/tasks/{key}/status", json={"status": "DONE", "evidence": "x", "actor": "founder"}, headers=headers)
        assert r.status_code in (401, 403)
    assert _state(engine, key)[0] == T.READY and _no_done(engine) == 0
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(models.AuditLog).where(models.AuditLog.action == "task.status")) == 0


def test_every_later_founder_decision_on_a_final_task_is_an_audited_change_never_a_silent_overwrite(engine):
    # The founder may reopen or re-decide a DONE task (main.py: closure and reopening are human-owned),
    # but each decision is its own audit row with its before/after, and an operator's attempt changes nothing.
    key = _task(engine)
    c = _api()
    assert c.post(f"/tasks/{key}/status", json={"status": "DONE", "evidence": "first verdict"}, headers=FOUNDER_H).status_code == 200
    assert c.post(f"/tasks/{key}/status", json={"status": "CANCELLED", "reason": "reopened"}, headers=FOUNDER_H).status_code == 200
    assert c.post(f"/tasks/{key}/status", json={"status": "DONE", "evidence": "second verdict"}, headers=FOUNDER_H).status_code == 200
    assert _state(engine, key)[0] == T.DONE
    refused = _api("http://testserver").post(f"/tasks/{key}/status", json={"status": "CANCELLED"}, headers=OPERATOR_H)
    assert refused.status_code == 403
    assert _state(engine, key)[0] == T.DONE
    with Session(engine) as db:
        rows = db.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == key,
                                                        models.AuditLog.action == "task.status")
                          .order_by(models.AuditLog.id)).all()
    assert len(rows) == 3
    assert [(r.before["status"], r.after["status"]) for r in rows] == [("READY", "DONE"), ("DONE", "CANCELLED"), ("CANCELLED", "DONE")]
    assert all(r.actor == f"founder:access:{FOUNDER}" for r in rows)
