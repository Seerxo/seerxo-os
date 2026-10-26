import asyncio
import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import api_auth, cloudflare_access


def test_signed_founder_identity_and_fail_closed_boundaries(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk["kid"] = "test-key"
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "test.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_DESK_AUD", "desk-aud")
    monkeypatch.setenv("CF_ACCESS_DESK_EMAILS", "founder@example.invalid")
    calls = []
    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk]})
    async def verify(token):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await cloudflare_access.verify_founder(token, client=client)
    claims = {"iss": "https://test.cloudflareaccess.com", "aud": ["desk-aud"],
              "exp": int(time.time()) + 300, "iat": int(time.time()), "type": "app",
              "sub": "founder-id", "email": "founder@example.invalid"}
    def token(changes=None, key=private, kid="test-key"):
        return jwt.encode({**claims, **(changes or {})}, key, algorithm="RS256", headers={"kid": kid})
    assert asyncio.run(verify(token())) is True
    for changes in ({"aud": ["other"]}, {"iss": "https://evil.invalid"},
                    {"exp": 1}, {"nbf": int(time.time()) + 600},
                    {"email": "other@example.invalid"}, {"type": "org"},
                    {"sub": "", "email": None}):
        assert asyncio.run(verify(token(changes))) is False
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert asyncio.run(verify(token(key=other))) is False
    assert asyncio.run(verify(token(kid="unknown"))) is False
    assert asyncio.run(verify("not-a-jwt")) is False
    assert asyncio.run(verify("")) is False
    assert asyncio.run(verify(jwt.encode(claims, "test-only-secret-long-enough-32-chars", algorithm="HS256"))) is False
    async def unavailable():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
            return await cloudflare_access.verify_founder(token(), client=client)
    assert asyncio.run(unavailable()) is False
    assert set(calls) == {"https://test.cloudflareaccess.com/cdn-cgi/access/certs"}
    monkeypatch.delenv("CF_ACCESS_DESK_EMAILS")
    assert asyncio.run(verify(token())) is False


def test_access_requests_never_inherit_loopback_admin_or_log_bypass(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "log")
    monkeypatch.setenv("CF_ACCESS_DESK_HOST", "desk.example.invalid")
    async def verify(token):
        return "founder@example.invalid" if token == "valid-founder" else None
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", verify)
    app = FastAPI()
    app.middleware("http")(api_auth.middleware)
    @app.get("/desk/state")
    @app.get("/tasks/TASK-1")
    @app.post("/desk/command")
    @app.post("/approvals/APR-1")
    @app.post("/registry")
    @app.post("/learnings")
    @app.post("/metrics/observations")
    @app.get("/")
    def endpoint(request: Request):
        return {"caller": request.state.caller}
    client = TestClient(app, base_url="https://desk.example.invalid", client=("127.0.0.1", 1234))
    headers = {"Cf-Access-Jwt-Assertion": "valid-founder"}
    assert client.get("/desk/state", headers=headers).json() == {"caller": "desk"}
    assert client.post("/approvals/APR-1", headers=headers).status_code == 200
    assert client.get("/tasks/TASK-1", headers=headers).status_code == 200
    assert client.post("/desk/command", headers=headers).status_code == 200
    assert client.post("/registry", headers=headers).status_code == 403
    assert client.post("/learnings", headers=headers).status_code == 403
    assert client.post("/metrics/observations", headers=headers).status_code == 403
    assert client.get("/desk/state").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": "bad"}).status_code == 401
    monkeypatch.setenv("COMPANY_API_TOKEN_DESK", "d" * 40)
    service_headers = {"Cf-Access-Jwt-Assertion": "service-assertion", "Authorization": "Bearer " + "d" * 40}
    # Old proxy path uses its own bearer, never service identity as founder.
    assert client.get("/desk/state", headers={**service_headers, "Host": "api.example.invalid"}).json() == {"caller": "desk"}
    assert client.get("/desk/state", headers=service_headers).status_code == 401
    monkeypatch.setenv("API_AUTH_MODE", "enforce")
    monkeypatch.setenv("API_HOST_IPS", "10.0.6.1")
    tunnel = TestClient(app, base_url="https://api.example.invalid", client=("10.0.6.4", 1234))
    assert tunnel.get("/desk/state", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    assert tunnel.get("/desk/state", headers=headers).json() == {"caller": "desk"}
    monkeypatch.setenv("COMPANY_API_TOKEN_ADMIN", "a" * 40)
    monkeypatch.setenv("COMPANY_API_TOKEN_YOUTUBE", "y" * 40)
    for token, status, who in (("a" * 40, 200, "admin"), ("y" * 40, 403, None), ("wrong", 401, None)):
        result = tunnel.get("/desk/state", headers={"Authorization": "Bearer " + token,
                                                    "Cf-Access-Jwt-Assertion": "service-assertion"})
        assert result.status_code == status
        if who:
            assert result.json() == {"caller": who}


def test_cached_keys_work_only_until_bounded_ttl_then_outage_denies(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk["kid"] = "cached-key"
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "test.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_DESK_AUD", "desk-aud")
    monkeypatch.setenv("CF_ACCESS_DESK_EMAILS", "founder@example.invalid")
    monkeypatch.setattr(cloudflare_access, "_cache", {})
    clock = [1000]
    monkeypatch.setattr(cloudflare_access.time, "monotonic", lambda: clock[0])
    responses = []
    def transport(request):
        responses.append(request.url)
        return httpx.Response(200, json={"keys": [jwk]}) if len(responses) == 1 else httpx.Response(503)
    factory = httpx.AsyncClient
    monkeypatch.setattr(cloudflare_access.httpx, "AsyncClient",
                        lambda **kwargs: factory(transport=httpx.MockTransport(transport), **kwargs))
    token = jwt.encode({"iss": "https://test.cloudflareaccess.com", "aud": ["desk-aud"],
                        "exp": int(time.time()) + 3600, "iat": int(time.time()),
                        "sub": "founder-id", "email": "founder@example.invalid", "type": "app"},
                       private, algorithm="RS256", headers={"kid": "cached-key"})
    assert asyncio.run(cloudflare_access.verify_founder(token)) is True
    clock[0] += 599
    assert asyncio.run(cloudflare_access.verify_founder(token)) is True
    assert len(responses) == 1
    clock[0] += 1
    assert asyncio.run(cloudflare_access.verify_founder(token)) is False
    assert len(responses) == 2


def test_api_serves_the_desk_uncached():
    from app import main
    client = TestClient(main.app, client=("127.0.0.1", 1234))
    for path in ("/", "/ui"):
        response = client.get(path)
        assert response.status_code == 200
        assert "/api/proxy" not in response.text
        assert response.headers["cache-control"] == "no-store"
