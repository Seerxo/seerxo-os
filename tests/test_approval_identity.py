"""Founder identity is proven by a verified Access assertion, never by a client-supplied string."""
import os
from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import api_auth, approval_identity as ai, cloudflare_access, enums, main, models

ADMIN, DESK, YT = "a" * 40, "d" * 40, "y" * 40
DESK_HOST = "desk.example.invalid"
FOUNDER = "founder@example.invalid"


def _app_db(engine):
    def get_db():
        with Session(engine) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.Approval(key="APR-0001", action="stage-c.cleanup TASK-0044 abc", reason="r", expected_effect="e",
                              risk_level=enums.RiskLevel.LOW, rollback="n", requested_by="ceo"))  # HIGH is tested in test_founder_gates
        s.commit()
    for k, v in {"COMPANY_API_TOKEN_ADMIN": ADMIN, "COMPANY_API_TOKEN_DESK": DESK, "COMPANY_API_TOKEN_YOUTUBE": YT,
                 "API_HOST_IPS": "10.0.6.1", "CF_ACCESS_DESK_HOST": DESK_HOST, "API_AUTH_MODE": "enforce"}.items():
        monkeypatch.setenv(k, v)
    valid = {"founder-assertion": FOUNDER}

    async def identity(token, **kw):
        return valid.get(token)
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)
    _app_db(engine)
    yield engine, valid
    main.app.dependency_overrides.clear()


def _client(host="172.18.0.5", base="http://testserver"):
    return TestClient(main.app, base_url=base, client=(host, 50000))


def _bearer(token):
    return {"authorization": f"Bearer {token}"}


def _state(engine):
    with Session(engine) as s:
        a = s.scalar(select(models.Approval))
        return (a.status, a.decided_by, a.decided_at, a.decision_notes,
                s.scalar(select(func.count()).select_from(models.AuditLog)))


DECIDE = {"granted": True, "decided_by": "founder", "notes": "forged"}


@pytest.mark.parametrize("headers,host,status", [
    ({}, "172.18.0.5", 401),                       # anonymous
    (_bearer(ADMIN), "172.18.0.5", 403),           # a generic admin token is not a person
    (_bearer(DESK), "172.18.0.5", 403),            # the legacy Desk token is not the founder
    (_bearer(YT), "172.18.0.5", 403),
    (_bearer("wrong"), "172.18.0.5", 401),
])
def test_forged_founder_string_is_refused_and_nothing_changes(env, headers, host, status):
    engine, _ = env
    before = _state(engine)
    for mode in ("enforce", "log"):  # rollout mode must not open the approval route
        os.environ["API_AUTH_MODE"] = mode
        r = _client(host).post("/approvals/APR-0001", json=DECIDE, headers=headers)
        assert r.status_code == status, (mode, r.text)
        assert _state(engine) == before
    os.environ["API_AUTH_MODE"] = "enforce"


def test_assertion_on_api_host_with_a_bearer_is_never_founder(env):
    engine, _ = env
    before = _state(engine)
    r = _client().post("/approvals/APR-0001", json=DECIDE,
                       headers={**_bearer(DESK), "Cf-Access-Jwt-Assertion": "founder-assertion"})
    assert r.status_code == 403 and _state(engine) == before  # bearer wins: service:desk, not founder


@pytest.mark.parametrize("host,who", [("127.0.0.1", "operator:local"), ("10.0.6.1", "operator:host")])
def test_operator_decides_as_itself_never_as_founder(env, host, who):
    engine, _ = env
    r = _client(host).post("/approvals/APR-0001", json=DECIDE)  # body claims "founder"
    assert r.status_code == 200 and r.json()["decided_by"] == who
    status, decided_by, decided_at, notes, _ = _state(engine)
    assert (status, decided_by) == (enums.ApprovalStatus.GRANTED, who) and decided_at
    with Session(engine) as s:
        entry = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "approval.decide"))
        assert entry.actor == who and entry.after["client_claimed_decided_by"] == "founder"
        assert not ai.verified_founder_decision(s, s.scalar(select(models.Approval)))


def test_verified_founder_assertion_is_the_only_founder_path(env):
    engine, _ = env
    c = _client(base=f"https://{DESK_HOST}")
    r = c.post("/approvals/APR-0001", json={"granted": True, "decided_by": "ceo-agent"},
               headers={"Cf-Access-Jwt-Assertion": "founder-assertion"})
    assert r.status_code == 200 and r.json()["decided_by"] == f"founder:access:{FOUNDER}"
    with Session(engine) as s:
        apr = s.scalar(select(models.Approval))
        assert apr.decided_by == f"founder:access:{FOUNDER}" and ai.verified_founder_decision(s, apr)
    assert c.post("/approvals/APR-0001", json=DECIDE, headers={"Cf-Access-Jwt-Assertion": "founder-assertion"}
                  ).status_code == 409  # a decided approval cannot be re-decided or overwritten


def test_rejection_is_not_founder_authority(env):
    engine, _ = env
    _client(base=f"https://{DESK_HOST}").post("/approvals/APR-0001", json={"granted": False},
                                              headers={"Cf-Access-Jwt-Assertion": "founder-assertion"})
    with Session(engine) as s:
        assert not ai.verified_founder_decision(s, s.scalar(select(models.Approval)))


@pytest.mark.parametrize("assertion", ["wrong", "", "revoked"])
def test_failed_or_revoked_assertion_changes_nothing(env, assertion):
    engine, valid = env
    before = _state(engine)
    c = _client(base=f"https://{DESK_HOST}")
    assert c.post("/approvals/APR-0001", json=DECIDE, headers={"Cf-Access-Jwt-Assertion": "founder-assertion"}
                  ).status_code == 200  # valid now ...
    with Session(engine) as s:  # reset the approval to compare the failure case against the same start
        apr = s.scalar(select(models.Approval))
        apr.status, apr.decided_by, apr.decided_at, apr.decision_notes = enums.ApprovalStatus.REQUESTED, None, None, None
        s.query(models.AuditLog).delete()
        s.commit()
    valid.pop("founder-assertion")  # ... revoked: the email left the allow-list / JWKS unavailable
    r = c.post("/approvals/APR-0001", json=DECIDE, headers={"Cf-Access-Jwt-Assertion": assertion or "x"})
    assert r.status_code == 401 and _state(engine)[1:] == (None, None, None, 0) and before[0] == enums.ApprovalStatus.REQUESTED


def test_legacy_forged_rows_are_not_founder_authority(env):
    engine, _ = env
    with Session(engine) as s:
        apr = s.scalar(select(models.Approval))
        apr.status, apr.decided_by, apr.decided_at = (enums.ApprovalStatus.GRANTED, "founder", datetime.now(timezone.utc))
        s.commit()
        assert not ai.verified_founder_decision(s, apr)  # the bare string is not the founder
        apr.decided_by = f"founder:access:{FOUNDER}"
        s.commit()
        assert not ai.verified_founder_decision(s, apr)  # prefix alone, no endpoint-written audit row
        s.add(models.AuditLog(actor="someone-else", action="approval.decide", entity_type="approval",
                              entity_key=apr.key, after={"status": "GRANTED"}))
        s.commit()
        assert not ai.verified_founder_decision(s, apr)  # audit row for a different actor


def test_sensitive_routes_stay_enforced_in_log_mode_but_others_keep_rollout_behaviour(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "log")
    app = FastAPI()
    app.middleware("http")(api_auth.middleware)

    @app.post("/approvals/APR-1")
    @app.post("/experiments")
    @app.post("/experiments/EXP-0001/approve")
    @app.post("/control/agents")
    @app.post("/rollout-probe")
    def endpoint(request: Request):
        return {"principal": request.state.principal.id}
    c = TestClient(app, client=("172.18.0.5", 1))
    # The agent kill switch joined the enforced set (review of #388): log mode never lets an anonymous caller flip it.
    for path in ("/approvals/APR-1", "/experiments", "/experiments/EXP-0001/approve", "/control/agents"):
        assert c.post(path).status_code == 401, path
        assert c.post(path, headers=_bearer("wrong")).status_code == 401
    assert c.post("/rollout-probe").status_code == 200  # non-sensitive: log mode still lets it through


def test_principals_never_trust_names():
    assert ai.from_caller("admin").id == "service:admin" and not ai.from_caller("admin").is_founder
    assert not ai.Principal("founder", "founder").is_founder  # a hand-built string is not enough
    assert ai.founder("A@B.co").id == "founder:access:a@b.co" and ai.founder("a@b.co").is_founder
    assert not ai.founder("not-an-email").is_founder  # a malformed email is no founder id
    assert ai.from_caller("anonymous") == ai.ANONYMOUS
    with pytest.raises(Exception):
        ai.require_founder(type("R", (), {"state": type("S", (), {})()})())  # no middleware -> no identity


# ---- PostgreSQL: two deciders serialize --------------------------------------------------------------

def test_postgres_concurrent_decisions_serialize(monkeypatch):
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=6)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            s.add(models.Approval(key="APR-0001", action="a", reason="r", expected_effect="e",
                                  risk_level=enums.RiskLevel.LOW, rollback="n", requested_by="ceo"))
            s.commit()
        monkeypatch.setenv("API_HOST_IPS", "10.0.6.1")
        _app_db(engine)
        gate, codes = Barrier(2), []

        def decide(granted):
            c = _client("127.0.0.1")
            gate.wait()
            codes.append(c.post("/approvals/APR-0001", json={"granted": granted}).status_code)
        threads = [Thread(target=decide, args=(g,)) for g in (True, False)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(codes) == [200, 409]
        with Session(engine) as s:
            assert s.scalar(select(func.count()).select_from(models.AuditLog)
                            .where(models.AuditLog.action == "approval.decide")) == 1
    finally:
        main.app.dependency_overrides.clear()
        models.Base.metadata.drop_all(engine)
        engine.dispose()
