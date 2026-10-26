"""Founder-only gates (task DONE, phase COMPLETE, high-risk approvals) use the verified principal, never `actor`."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import approval_identity as ai, cloudflare_access, enums, main, models

ADMIN, DESK, YT = "a" * 40, "d" * 40, "y" * 40
DESK_HOST, FOUNDER = "desk.example.invalid", "founder@example.invalid"
ASSERT = {"Cf-Access-Jwt-Assertion": "founder-assertion"}
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            models.Task(key="TASK-0001", title="t", success_criteria="s", status=enums.TaskStatus.REVIEW),
            models.Phase(number=7, name="p", status=enums.PhaseStatus.IN_PROGRESS),
            models.Approval(key="APR-LOW", action="a", reason="r", expected_effect="e", risk_level=enums.RiskLevel.LOW,
                            rollback="n", requested_by="ceo"),
            models.Approval(key="APR-HIGH", action="a", reason="r", expected_effect="e", risk_level=enums.RiskLevel.HIGH,
                            rollback="n", requested_by="ceo")])
        s.commit()
    for k, v in {"COMPANY_API_TOKEN_ADMIN": ADMIN, "COMPANY_API_TOKEN_DESK": DESK, "COMPANY_API_TOKEN_YOUTUBE": YT,
                 "API_HOST_IPS": "10.0.6.1", "CF_ACCESS_DESK_HOST": DESK_HOST, "API_AUTH_MODE": "enforce"}.items():
        monkeypatch.setenv(k, v)
    valid = {"founder-assertion": FOUNDER}

    async def identity(token, **kw):
        return valid.get(token)
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)

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


def _snap(engine):
    with Session(engine) as s:
        t, p = s.scalar(select(models.Task)), s.scalar(select(models.Phase))
        return (t.status, t.completed_at, t.result, p.status, p.completed_at, p.notes,
                [(a.status, a.decided_by) for a in s.scalars(select(models.Approval).order_by(models.Approval.id))],
                s.scalar(select(func.count()).select_from(models.AuditLog)))


DONE = {"status": "DONE", "evidence": "pytest output attached", "actor": "founder", "reason": "forged"}
COMPLETE = {"status": "COMPLETE", "evidence": "all deliverables verified", "actor": "founder"}
DENIED = [  # (label, headers, client host, expected status)
    ("anonymous", {}, "172.18.0.5", 401),
    ("admin token", _bearer(ADMIN), "172.18.0.5", 403),
    ("desk token", _bearer(DESK), "172.18.0.5", 403),
    ("youtube token", _bearer(YT), "172.18.0.5", 403),
    ("local operator", {}, "127.0.0.1", 403),
    ("host operator", {}, "10.0.6.1", 403),
]


@pytest.mark.parametrize("mode", ["enforce", "log"])
@pytest.mark.parametrize("label,headers,host,status", DENIED)
@pytest.mark.parametrize("route,body", [("/tasks/TASK-0001/status", DONE), ("/phases/7", COMPLETE)])
def test_forged_founder_actor_never_completes_work(env, monkeypatch, mode, label, headers, host, status, route, body):
    engine, _ = env
    monkeypatch.setenv("API_AUTH_MODE", mode)
    before = _snap(engine)
    r = _c(host).post(route, json=body, headers=headers)
    assert r.status_code in (status, 401, 403), (label, mode, r.text)  # middleware or handler, never 200
    assert r.status_code != 200 and _snap(engine) == before


@pytest.mark.parametrize("route,body", [("/tasks/TASK-0001/status", DONE), ("/phases/7", COMPLETE)])
def test_verified_founder_completes_and_is_audited_by_identity(env, route, body):
    engine, _ = env
    r = _founder().post(route, json={**body, "actor": "ceo-agent"}, headers=ASSERT)
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        entry = s.scalars(select(models.AuditLog).order_by(models.AuditLog.id.desc())).first()
        assert entry.actor == f"founder:access:{FOUNDER}"
        assert entry.after["client_claimed_actor"] == "ceo-agent"  # kept as untrusted evidence only
        assert s.scalar(select(models.Task)).status == (enums.TaskStatus.DONE if "tasks" in route else enums.TaskStatus.REVIEW)


def test_done_still_needs_evidence_and_a_valid_transition_for_the_founder(env):
    engine, _ = env
    before = _snap(engine)
    r = _founder().post("/tasks/TASK-0001/status", json={"status": "DONE"}, headers=ASSERT)
    assert r.status_code == 422 and _snap(engine) == before


@pytest.mark.parametrize("route,body", [("/tasks/TASK-0001/status", DONE), ("/phases/7", COMPLETE)])
@pytest.mark.parametrize("assertion", ["revoked", "garbage", None])
def test_revoked_or_invalid_founder_changes_nothing(env, route, body, assertion):
    engine, valid = env
    before = _snap(engine)
    valid.pop("founder-assertion")
    headers = {"Cf-Access-Jwt-Assertion": assertion} if assertion else {}
    assert _founder().post(route, json=body, headers=headers).status_code == 401
    assert _snap(engine) == before


def test_desk_token_cannot_use_the_founder_routes_even_with_an_assertion_attached(env):
    engine, _ = env
    before = _snap(engine)
    r = _c().post("/tasks/TASK-0001/status", json=DONE, headers={**_bearer(DESK), **ASSERT})
    assert r.status_code == 403 and _snap(engine) == before


@pytest.mark.parametrize("mode", ["enforce", "log"])
@pytest.mark.parametrize("label,headers,host,status", DENIED)
def test_notification_retry_requires_verified_founder(env, monkeypatch, mode, label, headers, host, status):
    engine, _ = env
    monkeypatch.setenv("API_AUTH_MODE", mode)
    before = _snap(engine)
    r = _c(host).post("/notifications/1/retry", headers=headers)
    assert r.status_code in (status, 401, 403), (label, mode, r.text)
    assert _snap(engine) == before


def test_non_privileged_changes_keep_working_and_record_the_real_caller(env):
    engine, _ = env
    with Session(engine) as s:
        task = s.scalar(select(models.Task))
        task.status = enums.TaskStatus.IN_PROGRESS
        s.commit()
    r = _c("127.0.0.1").post("/tasks/TASK-0001/status", json={"status": "BLOCKED", "actor": "founder", "error": "x"})
    assert r.status_code == 200
    r = _c("127.0.0.1").post("/phases/7", json={"status": "BLOCKED", "evidence": "waiting on a vendor", "actor": "founder"})
    assert r.status_code == 200
    with Session(engine) as s:
        rows = list(s.scalars(select(models.AuditLog).where(models.AuditLog.action.in_(("task.status", "phase.status")))))
        assert {x.actor for x in rows} == {"operator:local"} and all(x.after["client_claimed_actor"] == "founder" for x in rows)
    assert _c().post("/tasks/TASK-0001/status", json={"status": "BLOCKED"}).status_code == 401  # enforce: anonymous


# ---- approvals: an operator cannot grant what only the founder may grant --------------------------------

def test_operator_cannot_grant_high_risk_but_may_reject_it_and_grant_low_risk(env):
    engine, _ = env
    before = _snap(engine)
    r = _c("127.0.0.1").post("/approvals/APR-HIGH", json={"granted": True, "decided_by": "founder"})
    assert r.status_code == 403 and _snap(engine) == before
    assert _c("127.0.0.1").post("/approvals/APR-LOW", json={"granted": True}).status_code == 200
    assert _c("127.0.0.1").post("/approvals/APR-HIGH", json={"granted": False}).status_code == 200  # safe direction
    with Session(engine) as s:
        high = s.scalar(select(models.Approval).where(models.Approval.key == "APR-HIGH"))
        assert high.status == enums.ApprovalStatus.REJECTED and not ai.verified_founder_decision(s, high)


def test_founder_may_grant_high_risk_and_it_is_founder_authority(env):
    engine, _ = env
    assert _founder().post("/approvals/APR-HIGH", json={"granted": True}, headers=ASSERT).status_code == 200
    with Session(engine) as s:
        assert ai.verified_founder_decision(s, s.scalar(select(models.Approval).where(models.Approval.key == "APR-HIGH")))


def test_require_founder_approval_binds_task_action_and_age(env):
    engine, _ = env
    _founder().post("/approvals/APR-HIGH", json={"granted": True}, headers=ASSERT)
    with Session(engine) as s:
        task = s.scalar(select(models.Task))
        apr = s.scalar(select(models.Approval).where(models.Approval.key == "APR-HIGH"))
        apr.task_id = task.id
        s.commit()
        now = datetime.now(timezone.utc)
        assert ai.require_founder_approval(s, "APR-HIGH", action="a", task_id=task.id, now=now).key == "APR-HIGH"
        for kw in ({"action": "other"}, {"task_id": task.id + 1}, {"now": now + timedelta(days=2)}):
            with pytest.raises(PermissionError):
                ai.require_founder_approval(s, "APR-HIGH", **{"action": "a", "task_id": task.id, "now": now, **kw})
        assert _c("127.0.0.1").post("/approvals/APR-LOW", json={"granted": True}).status_code == 200
        with pytest.raises(PermissionError):  # an operator-granted approval is never founder authority
            ai.require_founder_approval(s, "APR-LOW", action="a")
        with pytest.raises(PermissionError):
            ai.require_founder_approval(s, "APR-NONE", action="a")
