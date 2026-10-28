"""Who may call which company-api route (founder approval 2026-10-05).

company-api sits on the youtube-auto network so that youtube-auto can report, which also
lets any process in that network reach every route. Each caller now has its own scope:

- local:   the API container's own loopback, i.e. `docker exec company-api curl localhost:8000`
           from host scripts (companyctl, backup, cron). Only root on the host can do that.
- admin:   COMPANY_API_TOKEN_ADMIN, for host callers using the published port.
- desk:    the Founder Desk's routes (DESK): a verified Cloudflare Access founder assertion,
           or COMPANY_API_TOKEN_DESK (kept for review; the Vercel proxy that used it is gone).
- host:    an address in API_HOST_IPS (comma list, empty by default): the company-os network
           gateway through which the published port 127.0.0.1:8099 reaches the API, i.e. the
           SSH-tunnel Desk and host scripts. Only processes on the host can originate from it.
- youtube: COMPANY_API_TOKEN_YOUTUBE, its reporting routes and the publish gate only.

API_AUTH_MODE=enforce (the default) refuses with 401/403; any value other than `log` is enforce.
log is rollout-only and must be set explicitly: it records what enforce would refuse and lets
it through, so a missing or mistyped variable can never reopen the API. Tokens come from the
environment and are never logged. The client address is the TCP peer: uvicorn runs with
--no-proxy-headers, so X-Forwarded-For cannot impersonate loopback.
"""
import hmac
import ipaddress
import logging
import os
import re

from fastapi.responses import JSONResponse
from starlette._utils import get_route_path  # private, but the exact function the router dispatches on

from app import approval_identity, cloudflare_access, metric_sources

log = logging.getLogger("company.api_auth")

PUBLIC = [("GET", r"/health"), ("GET", r"/"), ("GET", r"/ui"),
          ("GET", r"/manifest\.webmanifest"), ("GET", r"/sw\.js"),
          ("GET", r"/icons/(sez-192|sez-512|apple-touch-icon)\.png")]  # no operational data
DESK = [  # the only Desk policy; tests/test_api_auth.py pins it
    ("GET", r"/desk/state"), ("GET", r"/agents/[A-Za-z0-9_-]+"), ("GET", r"/tasks/[A-Za-z0-9_-]+"),
    ("GET", r"/company/brief"), ("GET", r"/desk/memory"), ("POST", r"/desk/command"),
    # Read-only trend history for the Desk charts (main.py desk_metric_series); without it every chart was 403.
    ("GET", r"/desk/metrics/series"),
    # Read-only change notices (app/desk_stream.py): the Desk updates when committed state changes, without polling.
    ("GET", r"/desk/stream"),
    ("POST", r"/tasks/[A-Za-z0-9_-]+/assign"), ("POST", r"/dispatch/run"),
    ("POST", r"/control/agents"), ("POST", r"/approvals/[A-Za-z0-9_-]+"),
]
YOUTUBE = [("POST", r"/webhooks/youtube-auto"), ("POST", r"/metrics/observations"),
           # The publish gate (youtube-auto paket-kontrol.js): submit a package, read its verdict.
           ("POST", r"/webhooks/youtube-packaging"), ("GET", r"/youtube/packaging/[^/]+")]
# Reachable only by a verified founder principal (Access assertion), on top of its scope: closing work is the
# founder's. The handlers re-check the principal, so log mode cannot widen these.
FOUNDER_SCOPE = [("POST", r"/control/agents"), ("POST", r"/tasks/[A-Za-z0-9_-]+/status"),
                 ("POST", r"/tasks/[A-Za-z0-9_-]+/run-report"), ("POST", r"/phases/[0-9]+"),
                 ("POST", r"/learnings/[0-9]+/approve"), ("POST", r"/stage-c/authorization"),
                 ("POST", r"/stage-c/authorization/revoke"),
                 ("POST", r"/stage-c/standing"), ("POST", r"/stage-c/standing/revoke"),
                 ("POST", r"/notifications/[0-9]+/retry")]
SCOPES = {"desk": DESK, "youtube": YOUTUBE}
TOKENS = {"admin": "COMPANY_API_TOKEN_ADMIN", "desk": "COMPANY_API_TOKEN_DESK",
          "youtube": "COMPANY_API_TOKEN_YOUTUBE"}
LOOPBACK = {"127.0.0.1", "::1"}
# A Host header is a hostname or [IPv6] literal with an optional port, nothing else. Starlette builds
# request.url from it, so "api:8000/health?x=" once made url.path differ from the routed path
# (GHSA-86qp-5c8j-p5mr) and "x@desk.host" can fake url.hostname (GHSA-jp82-jpqv-5vv3).
HOST_HEADER = re.compile(r"(?:\[([0-9A-Fa-f:.]+)\]|([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?))((?::[0-9]{1,5})?)")


def _host(header: str | None) -> tuple[bool, str]:
    """(valid, DNS name or ''). An absent Host (HTTP/1.0) is valid; a present one must be a name or IP."""
    if header is None:
        return True, ""
    m = HOST_HEADER.fullmatch(header)
    if not m or int(m.group(3)[1:] or 0) > 65535:
        return False, ""
    if m.group(1) is not None:
        try:
            ipaddress.IPv6Address(m.group(1))
        except ValueError:
            return False, ""
        return True, ""
    return True, _dns_name(m.group(2))


def _dns_name(name: str) -> str:
    """One spelling per DNS name: case-insensitive, and `desk.example.com.` is `desk.example.com`."""
    return name.strip().lower().removesuffix(".")
# What each token may write to /metrics/observations comes from app/metric_sources.py. A
# route scope alone would let a leaked youtube token report Seerxo revenue or signups, which
# goal progress and the business reconciler then act on. Trusted host callers (local, host,
# admin) are not limited here; internal-only sources (polar-api) are refused for everyone.
TRUSTED = ("local", "host", "admin")
# Routes whose authorization is never relaxed by API_AUTH_MODE=log (rollout mode would otherwise let an
# anonymous or out-of-scope caller reach them): approval decisions and every experiment mutation. Their
# handlers additionally require a verified founder principal where the action is the founder's
# (app/approval_identity.py).
ENFORCE_ALWAYS = [("POST", r"/approvals/[A-Za-z0-9_-]+"), ("POST", r"/learnings/[0-9]+/approve"),
                  # Task status and run reports change what the worker runs (READY re-queues work): never relaxed.
                  ("POST", r"/tasks/[A-Za-z0-9_-]+/status"), ("POST", r"/tasks/[A-Za-z0-9_-]+/run-report"),
                  # Creating READY work and the agent kill switch are privileged writes too (Codex review of #388).
                  ("POST", r"/tasks"), ("POST", r"/desk/command"), ("POST", r"/control/agents"),
                  # Events can release WAITING_EXTERNAL work (worker.release_external_waits); assignment picks the owner.
                  ("POST", r"/events"), ("POST", r"/tasks/[A-Za-z0-9_-]+/assign"),
                  # These also create or steer work: youtube-auto and incidents reach event_router (READY tasks),
                  # observations drive goals and the reconciler, goals and backup records are company state.
                  ("POST", r"/webhooks/youtube-auto"), ("POST", r"/incidents"), ("POST", r"/metrics/observations"),
                  ("POST", r"/goals"), ("PATCH", r"/goals/[0-9]+"), ("POST", r"/backups"),
                  ("POST", r"/stage-c/authorization"), ("POST", r"/stage-c/authorization/revoke"), ("POST", r"/experiments"),
                  ("POST", r"/stage-c/standing"), ("POST", r"/stage-c/standing/revoke"),
                  ("POST", r"/experiments/[A-Za-z0-9_-]+/[a-z-]+"),
                  ("POST", r"/notifications/[0-9]+/retry")]


def _match(routes, method: str, path: str) -> bool:
    return any(m == method and re.fullmatch(p, path) for m, p in routes)


def caller(client_host: str | None, authorization: str | None, environ=os.environ) -> str:
    """'local', a token's name, or 'anonymous'. A wrong token is anonymous, not an error."""
    if client_host in LOOPBACK:
        return "local"
    host_ips = {ip.strip() for ip in environ.get("API_HOST_IPS", "").split(",") if ip.strip()}
    if client_host in host_ips:
        return "host"
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and presented:
        for name, var in TOKENS.items():
            expected = environ.get(var, "")
            # Short or unset tokens never match, so an empty variable cannot open a scope.
            if len(expected) >= 32 and hmac.compare_digest(presented.encode(), expected.encode()):
                return name
    return "anonymous"


def enforcing() -> bool:
    return os.environ.get("API_AUTH_MODE", "enforce") != "log"


def metric_denial(who: str, unit: str, sources: set[str]) -> str | None:
    """Why `who` may not write these observations, or None when it may."""
    if who in TRUSTED:
        return None
    units, allowed_sources = metric_sources.writable(who)
    if unit not in units:
        return f"{who} may not write metrics for business unit '{unit}'"
    if not sources <= allowed_sources:
        return f"{who} may not write metrics as source(s) {sorted(sources - allowed_sources)}"
    return None


def allowed(who: str, method: str, path: str) -> bool:
    if who in TRUSTED or _match(PUBLIC, method, path):
        return True
    return _match(SCOPES.get(who, []), method, path)


async def middleware(request, call_next):
    # Authorize the path the router dispatches (scope path minus root_path), never request.url, which is
    # rebuilt from the Host header, whatever the Starlette version. A malformed Host is refused outright.
    method, path = request.method, get_route_path(request.scope)
    valid_host, host = _host(request.headers.get("host"))
    if not valid_host:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    assertion = request.headers.get("cf-access-jwt-assertion")
    desk_host = _dns_name(os.environ.get("CF_ACCESS_DESK_HOST", ""))
    is_desk_host = bool(desk_host and host == desk_host)
    authorization = request.headers.get("authorization")
    # The separate API hostname may add a service assertion to existing bearer
    # callers. They keep their own scopes; none can bypass founder verification on
    # the Desk hostname.
    bearer = caller(None, authorization)
    api_bearer = bearer != "anonymous" and not is_desk_host
    access_request = (assertion is not None and not api_bearer) or is_desk_host
    if access_request:
        # Access always fails closed, including rollout log mode and trusted TCP peers.
        # A browser/service assertion must never inherit the Tunnel peer's admin scope.
        email = await cloudflare_access.verify_founder_identity(assertion) if assertion else None
        if not email:
            return JSONResponse({"detail": "founder authentication required"}, status_code=401)
        who = "desk"
        principal = approval_identity.founder(email)  # the only path that yields a founder principal
    else:
        who = caller(request.client.host if request.client else None,
                     authorization)
        if api_bearer:
            who = bearer
        principal = approval_identity.from_caller(who)  # a credential or position, never a person
    request.state.caller = who  # routes that authorize the payload too (metrics) read it
    request.state.principal = principal
    if allowed(who, method, path) or (principal.is_founder and _match(FOUNDER_SCOPE, method, path)):
        return await call_next(request)
    if not access_request and not enforcing() and not _match(ENFORCE_ALWAYS, method, path):
        log.warning("api-auth WOULD_DENY caller=%s method=%s path=%s ip=%s", who, method, path,
                    request.client.host if request.client else None)
        return await call_next(request)
    status = 401 if who == "anonymous" else 403
    return JSONResponse({"detail": "authentication required" if status == 401
                         else f"{who} may not {method} {path}"}, status_code=status)
