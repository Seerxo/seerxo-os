"""Founder-only memory and Stage C authority: every path that writes LearningStatus.APPROVED, the Stage C
pilot authorization boundary, and the legacy-string records that must no longer count."""
import ast
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import approval_identity as ai, cloudflare_access, control, enums, main, models

ADMIN, DESK, YT = "a" * 40, "d" * 40, "y" * 40
DESK_HOST, FOUNDER = "desk.example.invalid", "founder@example.invalid"
FID = f"founder:access:{FOUNDER}"
ASSERT = {"Cf-Access-Jwt-Assertion": "founder-assertion"}
SHA = "c" * 40
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            models.Learning(title="cand", lesson="l", created_by="x", status=enums.LearningStatus.CANDIDATE),
            models.Incident(key="INC-0001", title="down", description="d", severity=enums.IncidentSeverity.SEV3,
                            status=enums.IncidentStatus.OPEN, detected_by="watchdog",
                            started_at=datetime.now(timezone.utc)),
            models.Task(key="TASK-0045", title="pilot", success_criteria="s", status=enums.TaskStatus.READY)])
        s.commit()
    for k, v in {"COMPANY_API_TOKEN_ADMIN": ADMIN, "COMPANY_API_TOKEN_DESK": DESK, "COMPANY_API_TOKEN_YOUTUBE": YT,
                 "API_HOST_IPS": "10.0.6.1", "CF_ACCESS_DESK_HOST": DESK_HOST, "API_AUTH_MODE": "enforce",
                 "COMPANY_OS_GIT_SHA": SHA}.items():
        monkeypatch.setenv(k, v)
    valid = {"founder-assertion": FOUNDER}

    async def identity(token, **kw):
        return valid.get(token)
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)
    monkeypatch.setattr(main.worker, "worker_state", lambda db: {"runtime": {"git_sha": SHA}})

    def get_db():
        with Session(engine) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db
    yield engine, valid
    main.app.dependency_overrides.clear()


def _c(host="172.18.0.5", base="http://testserver"):
    return TestClient(main.app, base_url=base, client=(host, 50000))


def _founder():
    return _c(base=f"https://{DESK_HOST}")


def _bearer(t):
    return {"authorization": f"Bearer {t}"}


CALLERS = [("anonymous", {}, "172.18.0.5"), ("admin", _bearer(ADMIN), "172.18.0.5"), ("desk token", _bearer(DESK), "172.18.0.5"),
           ("youtube", _bearer(YT), "172.18.0.5"), ("local", {}, "127.0.0.1"), ("host", {}, "10.0.6.1")]


def _mem(engine):
    with Session(engine) as s:
        return ([(x.id, x.status, x.created_by) for x in s.scalars(select(models.Learning).order_by(models.Learning.id))],
                s.scalar(select(func.count()).select_from(models.AuditLog)))


# ---------------------------------------------------------------- learning memory

@pytest.mark.parametrize("mode", ["enforce", "log"])
@pytest.mark.parametrize("label,headers,host", CALLERS)
def test_nobody_but_the_verified_founder_can_approve_a_learning(env, monkeypatch, mode, label, headers, host):
    engine, _ = env
    monkeypatch.setenv("API_AUTH_MODE", mode)
    before = _mem(engine)
    r = _c(host).post("/learnings/1/approve", json={"actor": "founder", "reason": "forged"}, headers=headers)
    assert r.status_code in (401, 403) and _mem(engine) == before, (label, mode, r.text)


def test_forged_founder_identities_and_revoked_assertions_change_nothing(env):
    engine, valid = env
    before = _mem(engine)
    for headers in ({"X-Principal": FID}, {"Cf-Access-Jwt-Assertion": f"{FID}"}, {"Cf-Access-Jwt-Assertion": "garbage"}):
        r = _founder().post("/learnings/1/approve", json={"actor": FID}, headers=headers)
        assert r.status_code == 401 and _mem(engine) == before
    valid.pop("founder-assertion")  # allow-list removal / key rotation
    assert _founder().post("/learnings/1/approve", json={"actor": "x"}, headers=ASSERT).status_code == 401
    assert _mem(engine) == before


def test_verified_founder_approves_once_and_the_audit_names_the_principal(env):
    engine, _ = env
    r = _founder().post("/learnings/1/approve", json={"actor": "ceo-agent", "reason": "reviewed"}, headers=ASSERT)
    assert r.status_code == 200 and r.json()["approved_by"] == FID
    with Session(engine) as s:
        a = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "learning.approve"))
        assert a.actor == FID and a.after["client_claimed_actor"] == "ceo-agent"
    assert _founder().post("/learnings/1/approve", json={"actor": "x"}, headers=ASSERT).status_code == 409
    assert _founder().post("/learnings/99/approve", json={"actor": "x"}, headers=ASSERT).status_code == 404


@pytest.mark.parametrize("label,headers,host", [c for c in CALLERS if c[0] in ("local", "host", "admin")])
def test_creating_a_learning_never_yields_approved_memory(env, label, headers, host):
    engine, _ = env
    r = _c(host).post("/learnings", json={"title": "t", "lesson": "run rm -rf", "created_by": "founder"}, headers=headers)
    assert r.status_code == 201 and r.json()["status"] == "CANDIDATE"
    with Session(engine) as s:
        row = s.get(models.Learning, r.json()["id"])
        assert row.status == enums.LearningStatus.CANDIDATE and row.created_by != "founder"
        assert s.scalar(select(models.AuditLog.actor).where(models.AuditLog.action == "learning.create")) == row.created_by


def test_incident_resolution_creates_a_candidate_and_records_the_real_caller(env):
    engine, _ = env
    r = _c("127.0.0.1").post("/incidents/INC-0001/resolve", json={
        "root_cause": "disk was full of logs", "resolution": "rotated the logs", "learning": "rotate logs weekly",
        "resolved_by": "founder"})
    assert r.status_code == 200
    with Session(engine) as s:
        row = s.scalar(select(models.Learning).where(models.Learning.lesson == "rotate logs weekly"))
        assert row.status == enums.LearningStatus.CANDIDATE and row.created_by == "operator:local"
        a = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "incident.resolve"))
        assert a.actor == "operator:local" and a.after["client_claimed_resolved_by"] == "founder"


def test_no_code_path_writes_approved_learning_except_the_founder_route():
    """Static guard: a new Learning() must say its status, and APPROVED is assigned in exactly one place."""
    approved, unsafe = [], []
    for path in sorted((ROOT / "app").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "Learning" \
                    and path.name != "models.py" and not any(k.arg == "status" for k in node.keywords):
                unsafe.append(f"{path.name}:{node.lineno}")
            writes = ((isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "status"
                                                              for t in node.targets) and ast.unparse(node.value))
                      or (isinstance(node, ast.keyword) and node.arg == "status" and ast.unparse(node.value)) or "")
            if "LearningStatus.APPROVED" in writes and path.name != "models.py":
                approved.append((path.name, getattr(node, "lineno", 0)))
    assert unsafe == []
    assert [a[0] for a in approved] == ["main.py"] and len(approved) == 1


def test_legacy_approved_rows_are_left_alone(env):
    engine, _ = env
    with Session(engine) as s:
        s.add(models.Learning(title="old", lesson="legacy", created_by="founder", status=enums.LearningStatus.APPROVED))
        s.commit()
    before = _mem(engine)
    assert _c("127.0.0.1").post("/learnings", json={"title": "n", "lesson": "n"}).status_code == 201
    assert [r for r in _mem(engine)[0] if r[2] == "founder"] == [r for r in before[0] if r[2] == "founder"]


# ---------------------------------------------------------------- Stage C pilot authorization

def _issue(engine, task="TASK-0045", sha=SHA, founder=FID, **kw):
    with Session(engine) as s:
        return control.authorize_pilot(s, task, sha, founder=founder, **kw)


def _auth_state(engine):
    with Session(engine) as s:
        row = s.get(models.ControlState, control.PILOT_AUTHORIZATION)
        return (dict(row.value), row.updated_by) if row else None, s.scalar(select(func.count()).select_from(models.AuditLog))


@pytest.mark.parametrize("mode", ["enforce", "log"])
@pytest.mark.parametrize("label,headers,host", CALLERS)
def test_only_the_verified_founder_can_issue_a_stage_c_authorization(env, monkeypatch, mode, label, headers, host):
    engine, _ = env
    monkeypatch.setenv("API_AUTH_MODE", mode)
    r = _c(host).post("/stage-c/authorization", json={"task_key": "TASK-0045", "minutes": 30}, headers=headers)
    assert r.status_code in (401, 403) and _auth_state(engine) == (None, 0), (label, mode)


def test_founder_issues_bound_to_the_deployed_sha_and_cannot_choose_it(env):
    engine, _ = env
    r = _founder().post("/stage-c/authorization", json={"task_key": "TASK-0045", "minutes": 20, "source_sha": "e" * 40},
                        headers=ASSERT)
    assert r.status_code == 201 and r.json()["source_sha"] == SHA and r.json()["issued_by"] == FID
    value, writer = _auth_state(engine)[0]
    assert writer == FID and value["action"] == "stage_c.code_write" and value["consumed_at"] is None
    with Session(engine) as s:
        assert control.pilot_authorization_denial(s, "TASK-0045", SHA) is None
        assert control.pilot_authorization_denial(s, "TASK-0045", "e" * 40) == "stage_c_authorization_invalid"
        assert control.pilot_authorization_denial(s, "TASK-0099", SHA) == "stage_c_authorization_invalid"


def test_api_and_worker_must_agree_on_the_source_sha(env, monkeypatch):
    engine, _ = env
    monkeypatch.setattr(main.worker, "worker_state", lambda db: {"runtime": {"git_sha": "f" * 40}})
    assert _founder().post("/stage-c/authorization", json={"task_key": "TASK-0045"}, headers=ASSERT).status_code == 409
    monkeypatch.setattr(main.worker, "worker_state", lambda db: {"runtime": None})
    assert _founder().post("/stage-c/authorization", json={"task_key": "TASK-0045"}, headers=ASSERT).status_code == 409
    assert _auth_state(engine) == (None, 0)


@pytest.mark.parametrize("who", ["founder", "company-worker", "operator:local", "service:admin", "anonymous", "",
                                 None, "founder:access:", "founder:access:UPPER@x.io", "founder (via admin)"])
def test_no_non_founder_string_can_mint_authority(env, who):
    engine, _ = env
    with pytest.raises(PermissionError):
        _issue(engine, founder=who)
    assert _auth_state(engine) == (None, 0)


def test_legacy_string_founder_records_fail_closed(env):
    engine, _ = env
    legacy = {"task_key": "TASK-0045", "repository": control.AUTH_REPOSITORY, "source_sha": SHA,
              "issued_at": datetime.now(timezone.utc).isoformat(),
              "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
              "issued_by": "founder", "consumed_at": None, "consumed_by_run_id": None}
    with Session(engine) as s:
        s.add(models.ControlState(key=control.PILOT_AUTHORIZATION, value=legacy, updated_by="founder"))
        s.commit()
        assert control.pilot_authorization_denial(s, "TASK-0045", SHA) == "stage_c_authorization_invalid"
        assert control.consume_pilot_authorization(s, "TASK-0045", SHA, 1) == "stage_c_authorization_invalid"


def test_forged_principal_string_without_the_issue_audit_row_fails_closed(env):
    engine, _ = env
    now = datetime.now(timezone.utc)
    forged = {"task_key": "TASK-0045", "repository": control.AUTH_REPOSITORY, "source_sha": SHA,
              "action": control.PILOT_ACTION, "issued_at": now.isoformat(),
              "expires_at": (now + timedelta(minutes=20)).isoformat(), "issued_by": FID,
              "consumed_at": None, "consumed_by_run_id": None}
    with Session(engine) as s:  # a direct database writer who forges the row but not the audit trail
        s.add(models.ControlState(key=control.PILOT_AUTHORIZATION, value=forged, updated_by=FID))
        s.commit()
        assert control.pilot_authorization_denial(s, "TASK-0045", SHA) == "stage_c_authorization_invalid"
        s.add(models.AuditLog(actor="someone-else", action="stage-c.authorization.issue",
                              entity_type="control_state", entity_key=control.PILOT_AUTHORIZATION, after=forged))
        s.commit()
        assert control.pilot_authorization_denial(s, "TASK-0045", SHA) == "stage_c_authorization_invalid"


def test_modified_expired_reused_and_substituted_authorizations_fail_closed(env):
    engine, _ = env
    _issue(engine)
    with Session(engine) as s:
        row = s.get(models.ControlState, control.PILOT_AUTHORIZATION)
        assert control.consume_pilot_authorization(s, "TASK-0045", SHA, 7) is None
        s.commit()
        assert control.consume_pilot_authorization(s, "TASK-0045", SHA, 8) == "stage_c_authorization_consumed"  # replay
        row = s.get(models.ControlState, control.PILOT_AUTHORIZATION)
        row.value = {**row.value, "consumed_at": None, "task_key": "TASK-0099"}  # substitution
        s.commit()
        assert control.pilot_authorization_denial(s, "TASK-0099", SHA) == "stage_c_authorization_invalid"
        row.value = {**row.value, "task_key": "TASK-0045", "expires_at": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()}
        s.commit()
        assert control.pilot_authorization_denial(s, "TASK-0045", SHA) == "stage_c_authorization_invalid"
    _issue(engine, now=datetime.now(timezone.utc) - timedelta(hours=1), ttl=timedelta(minutes=5))
    with Session(engine) as s:
        assert control.pilot_authorization_denial(s, "TASK-0045", SHA) == "stage_c_authorization_expired"


def test_revoke_is_open_to_a_verified_operator_or_founder_but_audits_who(env):
    engine, _ = env
    _issue(engine)
    assert _c("172.18.0.5").post("/stage-c/authorization/revoke", json={}, headers=_bearer(ADMIN)).status_code == 403
    assert _auth_state(engine)[0] is not None
    r = _c("127.0.0.1").post("/stage-c/authorization/revoke", json={"reason": "stop"})
    assert r.status_code == 200 and r.json() == {"revoked": True}
    with Session(engine) as s:
        assert s.scalar(select(models.AuditLog.actor).where(models.AuditLog.action == "stage-c.authorization.revoke")) == "operator:local"
    with Session(engine) as s:
        with pytest.raises(PermissionError):
            control.revoke_pilot_authorization(s, actor="founder")


def test_code_write_flag_is_still_off_in_deployment_config():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r'ENABLE_AUTONOMOUS_CODE_WRITE:\s*"false"', compose)
    assert "ENABLE_AUTONOMOUS_CODE_WRITE: \"true\"" not in compose


# ---------------------------------------------------------------- cleanup trust contract

def test_require_founder_approval_rejects_legacy_operator_and_forged_decisions(env):
    engine, _ = env
    with Session(engine) as s:
        task = s.scalar(select(models.Task))
        for key, by in (("APR-LEG", "founder"), ("APR-OP", "operator:local"), ("APR-FORGED", FID)):
            s.add(models.Approval(key=key, action="stage-c.cleanup TASK-0045 abc", reason="r", expected_effect="e",
                                  risk_level=enums.RiskLevel.HIGH, rollback="n", requested_by="ceo", task_id=task.id,
                                  status=enums.ApprovalStatus.GRANTED, decided_by=by, decided_at=datetime.now(timezone.utc)))
        s.commit()
        for key in ("APR-LEG", "APR-OP", "APR-FORGED", "APR-MISSING"):
            with pytest.raises(PermissionError):
                ai.require_founder_approval(s, key, action="stage-c.cleanup TASK-0045 abc", task_id=task.id)


def test_cleanup_approval_binds_task_action_digest_and_age_through_the_real_endpoint(env):
    engine, _ = env
    action = "stage-c.cleanup TASK-0045 abc"
    with Session(engine) as s:
        task = s.scalar(select(models.Task))
        s.add(models.Approval(key="APR-0001", action=action, reason="r", expected_effect="e", risk_level=enums.RiskLevel.HIGH,
                              rollback="n", requested_by="ceo", task_id=task.id))
        s.commit()
        tid = task.id
    assert _c("127.0.0.1").post("/approvals/APR-0001", json={"granted": True}).status_code == 403  # operator cannot grant HIGH
    assert _founder().post("/approvals/APR-0001", json={"granted": True, "decided_by": "ceo"}, headers=ASSERT).status_code == 200
    with Session(engine) as s:
        now = datetime.now(timezone.utc)
        assert ai.require_founder_approval(s, "APR-0001", action=action, task_id=tid, now=now).decided_by == FID
        for kw in ({"action": action + "x"}, {"task_id": tid + 1}, {"now": now + timedelta(hours=25)}):
            with pytest.raises(PermissionError):
                ai.require_founder_approval(s, "APR-0001", **{"action": action, "task_id": tid, "now": now, **kw})


# ---------------------------------------------------------------- critical route inventory

PROTECTED = {  # (method, path regex) -> required principal; asserted against the real route table
    "founder": [("POST", r"/tasks/[A-Za-z0-9_-]+/status"), ("POST", r"/phases/[0-9]+"),
                ("POST", r"/learnings/[0-9]+/approve"), ("POST", r"/stage-c/authorization")],
}


def test_critical_routes_are_enforce_always_and_reachable_by_the_founder_scope():
    from app import api_auth
    for method, path in PROTECTED["founder"]:
        probe = re.sub(r"\[[^\]]+\]\+", "X1", re.sub(r"\[0-9\]\+", "7", path))
        assert api_auth._match(api_auth.FOUNDER_SCOPE, method, probe), path
    for method, probe in (("POST", "/approvals/APR-1"), ("POST", "/learnings/7/approve"), ("POST", "/stage-c/authorization"),
                          ("POST", "/stage-c/authorization/revoke"), ("POST", "/experiments"),
                          ("POST", "/experiments/EXP-1/approve")):
        assert api_auth._match(api_auth.ENFORCE_ALWAYS, method, probe), probe
    routes = {(m, r.path) for r in main.app.routes for m in getattr(r, "methods", ())}
    assert ("POST", "/stage-c/authorization") in routes and ("POST", "/learnings/{learning_id}/approve") in routes


# ---------------------------------------------------------------- PostgreSQL

def test_postgres_concurrent_authorization_consumption_and_issue(monkeypatch):
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=8)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            s.add(models.Task(key="TASK-0045", title="p", success_criteria="s", status=enums.TaskStatus.READY))
            s.commit()
        _issue(engine)
        gate, results = Barrier(4), []

        def spend(i):
            with Session(engine) as s:
                gate.wait()
                results.append(control.consume_pilot_authorization(s, "TASK-0045", SHA, i))
                s.commit()
        ts = [Thread(target=spend, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results.count(None) == 1 and results.count("stage_c_authorization_consumed") == 3
        gate2, ids = Barrier(3), []

        def issue_again(i):
            gate2.wait()
            ids.append(_issue(engine)["issued_at"])
        ts = [Thread(target=issue_again, args=(i,)) for i in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        with Session(engine) as s:  # concurrent issues serialize on the row; the last writer's audit row matches the state
            assert control.pilot_authorization_denial(s, "TASK-0045", SHA) is None
    finally:
        models.Base.metadata.drop_all(engine)
        engine.dispose()


# ---------------------------------------------------------------- agent resume / audit identity

def _paused(engine):
    with Session(engine) as s:
        return control.agents_paused(s), s.scalar(select(func.count()).select_from(models.AuditLog))


RESUME = {"paused": False, "actor": "founder", "reason": "all clear"}


@pytest.mark.parametrize("mode", ["enforce", "log"])
@pytest.mark.parametrize("label,headers,host", CALLERS)
def test_only_the_verified_founder_can_resume_agents(env, monkeypatch, mode, label, headers, host):
    engine, _ = env
    with Session(engine) as s:
        control.set_agents_paused(s, True, actor="operator:local", reason="stop")
    monkeypatch.setenv("API_AUTH_MODE", mode)
    before = _paused(engine)
    r = _c(host).post("/control/agents", json=RESUME, headers=headers)
    assert r.status_code in (401, 403) and _paused(engine) == before, (label, mode)


def test_pause_stays_an_operator_emergency_stop_and_resume_names_the_founder(env):
    engine, _ = env
    r = _c("127.0.0.1").post("/control/agents", json={"paused": True, "actor": "founder", "reason": "runaway"})
    assert r.status_code == 200 and _paused(engine)[0] is True
    with Session(engine) as s:
        assert s.get(models.ControlState, "agents_paused").updated_by == "operator:local"
    assert _founder().post("/control/agents", json={**RESUME, "actor": "ceo"}, headers=ASSERT).status_code == 200
    with Session(engine) as s:
        assert control.agents_paused(s) is False
        resumed = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "agents.resume"))
        assert resumed.actor == FID


def test_goal_and_assignment_audit_the_verified_principal_not_the_claimed_actor(env):
    engine, _ = env
    with Session(engine) as s:
        s.add_all([models.Goal(title="g", metric="m", target="1", status=enums.GoalStatus.PROPOSED),
                   models.Agent(key="dev", role="dev", enabled=True)])
        s.commit()
    assert _c("127.0.0.1").patch("/goals/1", json={"title": "goal two", "actor": "founder"}).status_code == 200
    assert _c("127.0.0.1").post("/tasks/TASK-0045/assign", json={"agent": "dev", "actor": "founder"}).status_code == 200
    with Session(engine) as s:
        rows = list(s.scalars(select(models.AuditLog).where(models.AuditLog.action.in_(("goal.update", "task.assign")))))
        assert {r.actor for r in rows} == {"operator:local"} and all(r.after["client_claimed_actor"] == "founder" for r in rows)


# ---------------------------------------------------------------- experiment engine contract (routes belong to Agent 3)

def test_experiment_core_accepts_exactly_the_verified_principal_id():
    from app import experiments
    assert experiments.is_founder(ai.founder("founder@example.invalid").id)
    for fake in ("founder", "founder (via admin)", "operator:local", "service:admin", "anonymous",
                 "founder:access:", "founder:access:UPPER@example.invalid", ai.founder("x@y.io").id + " "):
        assert not experiments.is_founder(fake) and not ai.is_founder_id(fake)
    for who in ("founder", "operator:local", "service:admin"):
        with pytest.raises(experiments.NotFounder):
            experiments.approve_ready(None, None, who)
        with pytest.raises(experiments.NotFounder):
            experiments.decide(None, None, True, who, "r")


def test_the_scheduler_has_no_path_to_founder_only_steps():
    src = (ROOT / "app" / "experiment_scheduler.py").read_text()
    assert not re.search(r"\b(approve_ready|decide|propose_learning)\s*\(", src)
    assert "founder" not in re.sub(r"#.*|\"\"\".*?\"\"\"", "", src, flags=re.S).lower().replace("founder reviews", "")


def test_experiment_mutations_are_enforced_in_every_auth_mode():
    from app import api_auth
    for verb in ("approve", "start", "close-window", "evaluate", "accept", "reject", "cancel", "learning"):
        assert api_auth._match(api_auth.ENFORCE_ALWAYS, "POST", f"/experiments/EXP-0001/{verb}"), verb
    assert api_auth._match(api_auth.ENFORCE_ALWAYS, "POST", "/experiments")


def test_an_in_progress_task_cannot_be_reassigned_while_an_attempt_owns_it(env):
    # D1: a reassign mid-run would only block the running attempt; the API refuses it up front (409). The atomic
    # owner check in the worker still fails closed if a reassign races past this check.
    engine, _ = env
    with Session(engine) as s:
        s.add(models.Agent(key="dev", role="dev", enabled=True))
        s.scalar(select(models.Task).where(models.Task.key == "TASK-0045")).status = enums.TaskStatus.IN_PROGRESS
        s.commit()
    r = _c("127.0.0.1").post("/tasks/TASK-0045/assign", json={"agent": "dev", "actor": "founder"})
    assert r.status_code == 409 and "IN_PROGRESS" in r.json()["detail"]
    with Session(engine) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0045"))
        assert task.assigned_agent_id is None or task.assigned_agent_id != s.scalar(
            select(models.Agent.id).where(models.Agent.key == "dev"))
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.action == "task.assign")) is None
