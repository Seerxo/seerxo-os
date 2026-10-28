"""Caller scopes for company-api: youtube-auto's network neighbours cannot drive the company."""

import pytest
from fastapi.testclient import TestClient

from app import api_auth, main

TOK = {"COMPANY_API_TOKEN_ADMIN": "a" * 40, "COMPANY_API_TOKEN_DESK": "d" * 40,
       "COMPANY_API_TOKEN_YOUTUBE": "y" * 40}


def _who(host="172.18.0.5", auth=None, env=TOK):
    return api_auth.caller(host, auth, env)


def test_callers_are_identified_by_loopback_or_their_own_token():
    assert _who("127.0.0.1") == "local" and _who("::1") == "local"
    assert _who(auth="Bearer " + "y" * 40) == "youtube"
    assert _who(auth="Bearer " + "d" * 40) == "desk" and _who(auth="bearer " + "a" * 40) == "admin"
    # Break caught: a wrong, missing or malformed token gains a scope.
    for auth in (None, "Bearer wrong", "Basic " + "a" * 40, "Bearer", "Bearer " + "y" * 39):
        assert _who(auth=auth) == "anonymous", auth


def test_an_unset_or_short_token_variable_never_matches():
    # Break caught: COMPANY_API_TOKEN_DESK="" lets "Bearer " (empty) or a short guess in.
    assert _who(auth="Bearer ", env={"COMPANY_API_TOKEN_DESK": ""}) == "anonymous"
    assert _who(auth="Bearer abc", env={"COMPANY_API_TOKEN_DESK": "abc"}) == "anonymous"


@pytest.mark.parametrize("who, method, path, ok", [
    # The Desk reads trend history (read only). Break caught: every Desk chart answered 403 (live, 2026-10-16).
    ("desk", "GET", "/desk/metrics/series", True),
    # ...and nothing more: no write, no sibling or deeper path, no other caller gains it.
    ("desk", "POST", "/desk/metrics/series", False),
    ("desk", "GET", "/desk/metrics/series/extra", False),
    ("desk", "GET", "/desk/metrics", False),
    ("desk", "GET", "/metrics/observations", False),
    ("youtube", "GET", "/desk/metrics/series", False),
    ("anonymous", "GET", "/desk/metrics/series", False),
    # Live change notices (read only), and only that: no write, no deeper path, no other caller.
    ("desk", "GET", "/desk/stream", True),
    ("desk", "POST", "/desk/stream", False),
    ("desk", "GET", "/desk/stream/x", False),
    ("youtube", "GET", "/desk/stream", False),
    ("anonymous", "GET", "/desk/stream", False),
    ("youtube", "POST", "/metrics/observations", True),
    ("youtube", "POST", "/webhooks/youtube-auto", True),
    # Break caught: enforce blocks the publish gate, so no video can be uploaded.
    ("youtube", "POST", "/webhooks/youtube-packaging", True),
    ("youtube", "GET", "/youtube/packaging/2026-10-05-titanic", True),
    ("youtube", "GET", "/youtube/packaging/a/b", False),
    ("host", "POST", "/control/agents", True),
    # Break caught: a compromised youtube-auto pauses agents, approves, or creates tasks.
    ("youtube", "POST", "/control/agents", False),
    ("youtube", "POST", "/approvals/APR-1", False),
    ("youtube", "GET", "/desk/state", False),
    ("desk", "POST", "/approvals/APR-1", True),
    ("desk", "POST", "/metrics/observations", False),
    ("desk", "POST", "/tasks/TASK-1/assign/x", False),
    ("anonymous", "GET", "/health", True),
    ("anonymous", "GET", "/", True),
    ("anonymous", "GET", "/desk/state", False),
    ("anonymous", "POST", "/learnings", False),
    ("local", "POST", "/notes/compact", True),
    ("admin", "DELETE", "/anything", True),
])
def test_scopes(who, method, path, ok):
    assert api_auth.allowed(who, method, path) is ok


def test_desk_scope_is_exactly_the_founder_desk_routes():
    # The only Desk policy (the Vercel proxy's copy is gone). Break caught: the Desk scope
    # silently widens to another route, or loses one the Desk calls.
    assert set(api_auth.DESK) == {
        ("GET", r"/desk/state"), ("GET", r"/agents/[A-Za-z0-9_-]+"), ("GET", r"/tasks/[A-Za-z0-9_-]+"),
        ("GET", r"/company/brief"), ("GET", r"/desk/memory"), ("POST", r"/desk/command"),
        ("GET", r"/desk/metrics/series"), ("GET", r"/desk/stream"),
        ("POST", r"/tasks/[A-Za-z0-9_-]+/assign"), ("POST", r"/dispatch/run"),
        ("POST", r"/control/agents"), ("POST", r"/approvals/[A-Za-z0-9_-]+"),
    }


def _client(monkeypatch, mode):
    monkeypatch.setenv("API_AUTH_MODE", mode)
    for k, v in TOK.items():
        monkeypatch.setenv(k, v)
    return TestClient(main.app, client=("172.18.0.5", 50000))


def test_log_mode_lets_the_request_through_and_records_it(monkeypatch, caplog):
    # A non-privileged path: privileged writes (/control/agents, task status, task creation) are ENFORCE_ALWAYS now.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app import models
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)

    def db():
        with Session(engine) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = db
    c = _client(monkeypatch, "log")
    try:
        r = c.get("/notes")  # a real, non-sensitive route: log mode lets it through to the handler after logging it
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)
    assert r.status_code == 200 and r.json() == [], r.text
    assert "WOULD_DENY caller=anonymous method=GET path=/notes ip=172.18.0.5" in caplog.text
    assert "a" * 40 not in caplog.text


@pytest.mark.parametrize("mode", ["enforce", "typo"])
def test_enforce_refuses_and_an_unknown_mode_fails_closed(monkeypatch, mode):
    c = _client(monkeypatch, mode)
    assert c.post("/control/agents", json={}).status_code == 401
    r = c.post("/control/agents", json={}, headers={"Authorization": "Bearer " + "y" * 40})
    assert r.status_code == 403 and "youtube may not POST /control/agents" in r.json()["detail"]
    assert c.get("/").status_code == 200  # public page; /health needs a database here


def test_unset_mode_fails_closed(monkeypatch):
    c = _client(monkeypatch, "log")
    monkeypatch.delenv("API_AUTH_MODE")
    assert c.post("/control/agents", json={}).status_code == 401


def test_youtube_auto_calls_are_all_in_its_scope():
    # Every Company OS call youtube-auto makes (server-master, 2026-10-05).
    for method, path in [("POST", "/metrics/observations"), ("POST", "/webhooks/youtube-auto"),
                         ("POST", "/webhooks/youtube-packaging"),
                         ("GET", "/youtube/packaging/2026-10-05-titanic-s-last-night")]:
        assert api_auth.allowed("youtube", method, path), (method, path)


def test_the_host_gateway_is_trusted_only_when_configured():
    # The SSH-tunnel Desk and host scripts on the published port arrive from the company-os
    # network gateway (an example address here), never from loopback.
    env = {**TOK, "API_HOST_IPS": "10.0.6.1, 10.0.7.1"}
    assert api_auth.caller("10.0.6.1", None, env) == "host"
    assert api_auth.caller("10.0.7.1", None, env) == "host"
    # Break caught: an unset variable or a neighbour on another network counts as the host.
    assert api_auth.caller("10.0.6.1", None, TOK) == "anonymous"
    assert api_auth.caller("10.0.6.2", None, env) == "anonymous"
    assert api_auth.caller("10.0.6.1", None, {**TOK, "API_HOST_IPS": ""}) == "anonymous"


def _metric(unit, source):
    return {"business_unit": unit, "observations": [{
        "subject": "seerxo", "metric": "signups", "value": 100000, "source": source,
        "observed_at": "2026-10-05T00:00:00Z"}]}


@pytest.mark.parametrize("unit,source,why", [
    ("seerxo", "youtube-data-api", "business unit 'seerxo'"),
    ("youtube", "stripe", "source(s) ['stripe']"),
])
def test_youtube_token_writes_only_its_own_unit_and_sources(monkeypatch, unit, source, why):
    c = _client(monkeypatch, "enforce")
    yt = {"authorization": "Bearer " + "y" * 40}
    # Break caught: a leaked youtube token reports Seerxo signups that goals and the reconciler act on.
    r = c.post("/metrics/observations", json=_metric(unit, source), headers=yt)
    assert r.status_code == 403 and why in r.json()["detail"]
    assert api_auth.metric_denial("youtube", "youtube", {"youtube-data-api"}) is None
    assert api_auth.metric_denial("youtube", "youtube", {"youtube-analytics-api"}) is None
    assert api_auth.metric_denial("youtube", "seerxo", {"youtube-analytics-api"}) is not None
    assert api_auth.metric_denial("admin", "seerxo", {"anything"}) is None
    assert api_auth.metric_denial("desk", "youtube", {"youtube-data-api"}) is not None


@pytest.mark.parametrize("unit,source", [("seerxo", "youtube-data-api"), ("youtube", "stripe")])
def test_log_mode_refuses_out_of_scope_metrics_without_writing(monkeypatch, unit, source):
    from app import metrics_store

    def forbidden_write(db, body):
        pytest.fail("an out-of-scope observation reached the metrics writer")

    monkeypatch.setattr(metrics_store, "record", forbidden_write)
    main.app.dependency_overrides[main.get_db] = lambda: None
    try:
        c = _client(monkeypatch, "log")
        r = c.post("/metrics/observations", json=_metric(unit, source),
                   headers={"authorization": "Bearer " + "y" * 40})
        assert r.status_code == 403, r.text
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("mode", ["log", "enforce"])
@pytest.mark.parametrize("caller,unit,source", [("youtube", "youtube", "youtube-data-api"),
                                               ("host", "seerxo", "google-analytics")])
def test_allowed_metrics_still_reach_the_writer(monkeypatch, mode, caller, unit, source):
    from app import metrics_store
    monkeypatch.setattr(metrics_store, "record", lambda db, body: {"inserted": 1})
    monkeypatch.setenv("API_HOST_IPS", "172.18.0.5" if caller == "host" else "")
    main.app.dependency_overrides[main.get_db] = lambda: None
    try:
        r = _client(monkeypatch, mode).post(
            "/metrics/observations", json=_metric(unit, source),
            headers={"authorization": "Bearer " + "y" * 40} if caller == "youtube" else {})
        assert r.status_code == 201 and r.json() == {"inserted": 1}
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("method, path, host, auth", [
    # GHSA-86qp-5c8j-p5mr: Starlette < 1.0.1 rebuilt url.path from the Host header, so the middleware saw a
    # PUBLIC path while the router dispatched a private one. Break caught: any network neighbour reads the Desk.
    ("GET", "/desk/state", "testserver/health?x=", None),
    ("GET", "/desk/state", "testserver:8000/?x=", None),
    ("GET", "/desk/state", "testserver/sw.js#", None),
    # ...or a youtube token reaches a route outside its scope by naming one of its own in Host.
    ("POST", "/approvals/APR-1", "testserver/webhooks/youtube-auto?x=", "y"),
    ("POST", "/control/agents", "testserver/metrics/observations#", "y"),
])
def test_a_crafted_host_header_cannot_change_the_authorized_path(monkeypatch, method, path, host, auth):
    from app import approval_identity
    monkeypatch.setattr(approval_identity, "founder", lambda email: pytest.fail("reached Access"))
    c = _client(monkeypatch, "enforce")
    headers = {"host": host, **({"authorization": "Bearer " + auth * 40} if auth else {})}
    r = c.request(method, path, json={}, headers=headers)
    assert r.status_code in (400, 401, 403), (r.status_code, r.text)


@pytest.mark.parametrize("host", ["evil@desk.example.com", "desk.example.com/x", "a b", "desk.example.com:80:80",
                                  "desk.example.com:99999", "desk.example.com:", "desk.example.com..",
                                  "[::::]", "[desk.example.com]", "", ".desk.example.com"])
def test_a_malformed_host_header_is_refused(monkeypatch, host):
    # GHSA-jp82-jpqv-5vv3 class: the Desk-host decision must not come from a Host that is not a hostname.
    monkeypatch.setenv("CF_ACCESS_DESK_HOST", "desk.example.com")
    r = _client(monkeypatch, "enforce").get("/", headers={"host": host})
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("host", ["testserver", "testserver:8000", "API:8000", "company-api:8000", "testserver.",
                                  "127.0.0.1:8099", "[::1]:8099"])
def test_ordinary_host_headers_still_work(monkeypatch, host):
    assert _client(monkeypatch, "enforce").get("/", headers={"host": host}).status_code == 200


def test_the_desk_host_is_matched_on_the_host_header_without_its_port(monkeypatch):
    # Break caught: dropping url.hostname lets a Desk-host request skip founder verification.
    monkeypatch.setenv("CF_ACCESS_DESK_HOST", "desk.example.com")
    c = _client(monkeypatch, "enforce")
    # A trailing dot is the same DNS name (Codex review): a Desk token must not skip Access through it.
    for host in ("desk.example.com", "DESK.example.com:443", "desk.example.com.", "desk.example.com.:443"):
        for token in ("a", "d"):
            r = c.get("/desk/state", headers={"host": host, "authorization": "Bearer " + token * 40})
            assert r.status_code == 401 and r.json()["detail"] == "founder authentication required", (host, token)


def test_scopes_follow_the_routed_path_under_a_root_path(monkeypatch):
    # A deployment behind a path prefix (uvicorn --root-path): the router strips root_path, so must the check.
    # Break caught: matching the raw scope path refuses every public route, or a prefix smuggles a private one.
    for k, v in TOK.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("API_AUTH_MODE", "enforce")
    c = TestClient(main.app, root_path="/api", client=("172.18.0.5", 50000))
    assert c.get("/api/").status_code == 200
    assert c.get("/api/sw.js").status_code == 200
    assert c.get("/api/desk/state").status_code == 401
    # ...and a scoped caller still reaches a private route under the prefix (past the middleware: no 401/403;
    # the handler then fails on the stub database, which is fine here).
    main.app.dependency_overrides[main.get_db] = lambda: None
    try:
        r = TestClient(main.app, root_path="/api", client=("172.18.0.5", 50000), raise_server_exceptions=False).get(
            "/api/desk/state", headers={"authorization": "Bearer " + "d" * 40})
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)
    assert r.status_code not in (400, 401, 403, 404), r.text
