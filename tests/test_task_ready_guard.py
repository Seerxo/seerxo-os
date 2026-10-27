"""POST /tasks/{key}/status with READY (the Desk's "Mark READY"): only the verified founder or a host operator reaches
it, a task with an open agent run is never re-queued, and a code-writing (Stage C) task becomes READY only when every
Stage C condition holds now: agents not paused, host acceptance and a fresh preflight for this SHA, an unexpired
authorization for it, an enabled owner. Every refusal names its reason and writes nothing."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cloudflare_access, control, enums, main, models, stage_c_worker, worker

SHA = "a" * 40
DESK_HOST, FOUNDER = "desk.example.invalid", "founder@example.invalid"
FID = f"founder:access:{FOUNDER}"
T = enums.TaskStatus


@pytest.fixture
def engine(monkeypatch, tmp_path):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    with Session(eng) as db:
        for key in ("developer", "research"):
            db.add(models.Agent(key=key, role=key, enabled=True))
        db.add(models.Project(key="company-os", name="Company OS", repository="seerxo/seerxo-os", status=enums.ProjectStatus.ACTIVE))
        db.commit()
    for k, v in {"COMPANY_API_TOKEN_DESK": "d" * 40, "CF_ACCESS_DESK_HOST": DESK_HOST, "API_AUTH_MODE": "enforce",
                 "COMPANY_OS_GIT_SHA": SHA}.items():
        monkeypatch.setenv(k, v)

    async def identity(token, **kw):
        return FOUNDER if token == "founder" else None
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)

    def get_db():
        with Session(eng) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db
    yield eng
    main.app.dependency_overrides.clear()


def _task(engine, key, status, write=False, agent="developer"):
    with Session(engine) as db:
        a = db.scalar(select(models.Agent).where(models.Agent.key == agent)) if agent else None
        p = db.scalar(select(models.Project).where(models.Project.key == "company-os"))
        ctx = {"autonomous": True, "write": True, "stage_c": {"paths": ["tests/test_metrics.py"]}} if write else {"autonomous": True}
        db.add(models.Task(key=key, title="t", success_criteria="evidence present", objective="o", status=status,
                           risk_level=enums.RiskLevel.LOW, assigned_agent_id=a.id if a else None,
                           project_id=p.id if write else None, context=ctx))
        db.commit()


def _ready(key, founder=True):
    c = TestClient(main.app, base_url=f"https://{DESK_HOST}" if founder else "http://testserver", client=("172.18.0.5", 50000))
    return c.post(f"/tasks/{key}/status", json={"status": "READY", "actor": "founder", "reason": "Mark READY"},
                  headers={"Cf-Access-Jwt-Assertion": "founder"} if founder else {"authorization": f"Bearer {'d' * 40}"})


def _state(engine, key):
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == key))
        audits = db.scalar(select(func.count()).select_from(models.AuditLog).where(models.AuditLog.entity_key == key,
                                                                               models.AuditLog.action == "task.status"))
        return t.status, audits


def _stage_c_ready(engine, *, authorize=True, minutes=30, now=None, code_write=True, preflight_age=1):
    with Session(engine) as db:
        worker._heartbeat(db, autonomy={"code_write_flag": code_write})
        db.add(models.ControlState(key="stage-c-host-acceptance", updated_by="founder", value={
            "source_sha": SHA, "checks": dict.fromkeys(stage_c_worker.ACCEPTANCE_CHECKS, True)}))
        db.add(models.ControlState(key="stage-c-runtime-readiness", updated_by="watchdog", value={
            "ready": True, "checked_at": (datetime.now(timezone.utc) - timedelta(minutes=preflight_age)).isoformat(), "source_sha": SHA, "failed": []}))
        db.commit()
        if authorize:
            control.authorize_pilot(db, "TASK-0010", SHA, founder=FID, ttl=timedelta(minutes=minutes), now=now)


def test_founder_marks_a_blocked_task_ready_and_it_is_audited_as_the_founder(engine):
    _task(engine, "TASK-0001", T.BLOCKED)
    r = _ready("TASK-0001")
    assert r.status_code == 200, r.text
    with Session(engine) as db:
        row = db.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0001"))
        assert row.actor == FID and row.after["status"] == "READY"   # the verified identity, not the body's "founder"


def test_the_desk_token_alone_cannot_mark_ready(engine):
    _task(engine, "TASK-0001", T.BLOCKED)
    assert _ready("TASK-0001", founder=False).status_code in (401, 403)
    assert _state(engine, "TASK-0001") == (T.BLOCKED, 0)


def test_a_task_with_an_open_agent_run_is_never_requeued(engine):
    _task(engine, "TASK-0002", T.BLOCKED)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0002"))
        db.add(models.AgentRun(task_id=t.id, provider="claude", status=enums.AgentRunStatus.STARTED)); db.commit()
    r = _ready("TASK-0002")
    assert r.status_code == 409 and "agent run for it is still open" in r.json()["detail"]
    assert _state(engine, "TASK-0002") == (T.BLOCKED, 0)


@pytest.mark.parametrize("setup, reason", [
    ("paused", "agents are paused"),
    ("code_write_off", "autonomous code writing is off"),
    ("stale_preflight", "stage_c_runtime_not_ready"),
    ("disabled_owner", "is disabled"),
    ("published_pr", "published pull request"),
    ("other_task_authorized", "authorization"),
    ("no_acceptance", "stage_c_host_not_accepted"),
    ("no_authorization", "authorization"),
    ("expired", "authorization"),
    ("no_owner", "owner"),
])
def test_a_code_writing_task_needs_every_stage_c_condition_now(engine, setup, reason):
    _task(engine, "TASK-0010", T.BLOCKED, write=True, agent=None if setup == "no_owner" else "developer")
    if setup == "no_acceptance":
        with Session(engine) as db:
            worker._heartbeat(db, autonomy={"code_write_flag": True})
    else:
        _stage_c_ready(engine, authorize=setup not in ("no_authorization", "other_task_authorized"),
                       now=datetime.now(timezone.utc) - timedelta(minutes=45) if setup == "expired" else None,
                       code_write=setup != "code_write_off", preflight_age=60 if setup == "stale_preflight" else 1)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0010"))
        if setup == "disabled_owner":
            db.get(models.Agent, t.assigned_agent_id).enabled = False
        if setup == "published_pr":
            t.context = {**t.context, "improvement": {"stage": "PR_OPEN", "history": [{"stage": "PR_OPEN", "evidence": {"pr": "https://github.com/seerxo/seerxo-os/pull/9"}}]}}
        if setup == "other_task_authorized":
            _task(engine, "TASK-0011", T.BLOCKED, write=True)
            control.authorize_pilot(db, "TASK-0011", SHA, founder=FID)
        db.commit()
    if setup == "paused":
        with Session(engine) as db:
            control.set_agents_paused(db, True, actor=FID, reason="test"); db.commit()
    r = _ready("TASK-0010")
    assert r.status_code == 409, r.text
    assert reason in r.json()["detail"].lower() or reason in r.json()["detail"]
    assert _state(engine, "TASK-0010") == (T.BLOCKED, 0)


def test_a_code_writing_task_with_every_condition_met_becomes_ready(engine):
    _task(engine, "TASK-0010", T.BLOCKED, write=True)
    _stage_c_ready(engine)
    r = _ready("TASK-0010")
    assert r.status_code == 200, r.text
    assert _state(engine, "TASK-0010") == (T.READY, 1)


def test_an_in_progress_task_with_no_open_run_may_be_requeued_but_never_one_with_a_live_run(engine):
    """The deliberate line: a claim whose worker died (no run) can be recovered; a live run is never doubled. A refused
    READY writes nothing, so the live run still holds its attempt (PR-1, worker.holds_attempt)."""
    _task(engine, "TASK-0020", T.READY)
    _task(engine, "TASK-0021", T.READY)
    with Session(engine) as db:
        stuck = db.scalar(select(models.Task).where(models.Task.key == "TASK-0020"))
        live = db.scalar(select(models.Task).where(models.Task.key == "TASK-0021"))
        assert worker.claim(db, stuck) and worker.claim(db, live)
        run = models.AgentRun(task_id=live.id, agent_id=live.assigned_agent_id, provider="claude", status=enums.AgentRunStatus.STARTED)
        db.add(run); db.commit()
        run_id = run.id
    assert _ready("TASK-0020").status_code == 200
    r = _ready("TASK-0021")
    assert r.status_code == 409 and "still open" in r.json()["detail"]
    with Session(engine) as db:
        live = worker._locked_task(db, db.scalar(select(models.Task.id).where(models.Task.key == "TASK-0021")))
        assert worker.holds_attempt(db, live, db.get(models.AgentRun, run_id))


def test_a_cancelled_herdr_report_records_the_run_but_never_requeues_past_the_guard(engine):
    _task(engine, "TASK-0030", T.IN_PROGRESS)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0030"))
        db.add(models.AgentRun(task_id=t.id, provider="claude", status=enums.AgentRunStatus.STARTED)); db.commit()
    c = TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000))
    r = c.post("/tasks/TASK-0030/run-report", headers={"Cf-Access-Jwt-Assertion": "founder"},
               json={"herdr_state": {"state": "cancelled", "workflow": {"name": "w"}, "created_at": "2026-10-20T10:00:00Z"},
                     "reported_by": "herdr", "changed_files": []})
    assert r.status_code == 201, r.text
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0030"))
        assert t.status == T.IN_PROGRESS                      # not re-queued over the live run
        row = db.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0030", models.AuditLog.action == "task.run-report"))
        assert "still open" in row.after["requeue_refused"]


# Codex review of #388, finding 5: the documented recovery of an interrupted CI repair ("set READY to retry",
# stage_c_worker.recover_interrupted) was always refused by the published-PR rule.
def _interrupted_repair(engine):
    _task(engine, "TASK-0010", T.BLOCKED, write=True)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0010"))
        t.context = {**t.context, "improvement": {"stage": "IMPLEMENTING", "history": [
            {"stage": "PR_OPEN", "evidence": {"pr": "https://github.com/seerxo/seerxo-os/pull/9"}}]}}
        t.error = f"{stage_c_worker.REPAIR_INTERRUPTED} worker lost a CI repair of https://github.com/seerxo/seerxo-os/pull/9 at IMPLEMENTING"
        db.commit()


def test_an_interrupted_ci_repair_can_be_retried_once_stopped(engine):
    _interrupted_repair(engine)
    _stage_c_ready(engine)
    r = _ready("TASK-0010")
    assert r.status_code == 200, r.text
    assert _state(engine, "TASK-0010") == (T.READY, 1)


@pytest.mark.parametrize("setup, reason", [
    ("lease_held", "lease is still held"),
    ("consumed", "consumed"),
    ("open_run", "still open"),
])
def test_an_interrupted_ci_repair_is_never_retried_while_it_may_still_run_or_twice(engine, setup, reason):
    _interrupted_repair(engine)
    _stage_c_ready(engine)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0010"))
        if setup == "lease_held":
            from app import stage_c
            db.add(models.ControlState(key=stage_c.lease_key(stage_c_worker.REPOSITORY), updated_by=f"stage-c-runner:{t.key}",
                                       value={"token": 1, "holder": f"stage-c-runner:{t.key}",
                                              "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}))
        if setup == "consumed":
            row = db.get(models.ControlState, control.PILOT_AUTHORIZATION)
            row.value = {**row.value, "consumed_at": datetime.now(timezone.utc).isoformat()}
        if setup == "open_run":
            db.add(models.AgentRun(task_id=t.id, agent_id=t.assigned_agent_id, provider="claude", status=enums.AgentRunStatus.STARTED))
        db.commit()
    r = _ready("TASK-0010")
    assert r.status_code == 409 and reason in r.json()["detail"], r.text
    assert _state(engine, "TASK-0010") == (T.BLOCKED, 0)


def test_any_other_published_pr_block_is_still_not_requeued_by_hand(engine):
    _interrupted_repair(engine)
    _stage_c_ready(engine)
    with Session(engine) as db:
        db.scalar(select(models.Task).where(models.Task.key == "TASK-0010")).error = f"{stage_c_worker.PR_CLOSED}: #9"
        db.commit()
    r = _ready("TASK-0010")
    assert r.status_code == 409 and "published pull request" in r.json()["detail"]


def test_a_status_caller_cannot_forge_or_erase_a_worker_error_marker(engine):
    """Independent review P2-a: the retry exception rests on the structured stage (stage_c_worker.blocked_repair),
    and the worker's error markers can be neither written nor overwritten through the status route."""
    _interrupted_repair(engine)
    c = TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000))
    h = {"Cf-Access-Jwt-Assertion": "founder"}
    assert c.post("/tasks/TASK-0010/status", headers=h, json={"status": "BLOCKED", "error": "anything"}).status_code == 422
    _task(engine, "TASK-0011", T.BLOCKED, write=True)
    assert c.post("/tasks/TASK-0011/status", headers=h, json={"status": "BLOCKED",
                  "error": f"{stage_c_worker.REPAIR_INTERRUPTED} forged"}).status_code == 422
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0011"))
        assert t.error is None


def test_a_published_task_past_the_repair_stages_is_never_a_retryable_repair(engine):
    """The probe from P2-a: a CI-stage task given the interrupted-repair text is still refused (stage, not text)."""
    _interrupted_repair(engine)
    _stage_c_ready(engine)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == "TASK-0010"))
        t.context = {**t.context, "improvement": {**t.context["improvement"], "stage": "CI"}}
        db.commit()
    r = _ready("TASK-0010")
    assert r.status_code == 409 and "published pull request" in r.json()["detail"]


def test_an_expired_lease_does_not_hold_back_a_stopped_repair(engine):
    _interrupted_repair(engine)
    _stage_c_ready(engine)
    with Session(engine) as db:
        from app import stage_c
        db.add(models.ControlState(key=stage_c.lease_key(stage_c_worker.REPOSITORY), updated_by="stage-c-runner:TASK-0010",
                                   value={"token": 1, "holder": "stage-c-runner:TASK-0010",
                                          "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}))
        db.commit()
    assert _ready("TASK-0010").status_code == 200


def test_only_the_founder_retries_a_stopped_repair(engine):
    """Founder decision 2026-10-21: re-coding a stopped CI repair is the founder's; a host operator may not."""
    _interrupted_repair(engine)
    _stage_c_ready(engine)
    op = TestClient(main.app, base_url="http://testserver", client=("127.0.0.1", 50000))   # loopback = operator:local
    r = op.post("/tasks/TASK-0010/status", json={"status": "READY", "actor": "founder", "reason": "x"})
    assert r.status_code in (401, 403), r.text
    assert _state(engine, "TASK-0010") == (T.BLOCKED, 0)
    assert _ready("TASK-0010").status_code == 200


def test_an_operator_run_report_never_requeues_a_stopped_repair(engine):
    """Review of #402: the run-report requeue path is founder-only for a stopped CI repair, like the status route.
    The operator's cancelled report is still recorded; the task stays BLOCKED and the refusal is in the audit row."""
    _interrupted_repair(engine)
    _stage_c_ready(engine)
    op = TestClient(main.app, base_url="http://testserver", client=("127.0.0.1", 50000))   # loopback = operator:local
    r = op.post("/tasks/TASK-0010/run-report",
                json={"herdr_state": {"state": "cancelled", "workflow": {"name": "w"}, "created_at": "2026-10-20T10:00:00Z"},
                      "reported_by": "herdr", "changed_files": []})
    assert r.status_code == 201, r.text
    assert _state(engine, "TASK-0010") == (T.BLOCKED, 0)
    with Session(engine) as db:
        row = db.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0010",
                                                      models.AuditLog.action == "task.run-report"))
        assert row.actor == "operator:local" and "founder's decision" in row.after["requeue_refused"]
