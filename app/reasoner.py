"""Stage B model runtime, outside the control plane (company-reasoner container).

The worker holds DATABASE_URL, the Telegram token and the Stage C GitHub token. Filtering
the child environment (providers.child_env) is defence in depth, not a boundary: a model
CLI in the same container can still read the container's files and reach its networks.
So the CLIs run here instead, with only the provider logins: no database network, no
control-plane secrets, no Docker socket. The worker sends one bounded JSON request and
gets a ProviderResult back (providers.RemoteProvider).

    POST /run     {provider, prompt, schema, timeout_s, max_turns} -> ProviderResult
    GET  /health  {provider: {health, why, version}}

Reachable only on the reasoner network, which only company-worker joins.
ponytail: network membership is the caller check; add a shared token if a second service joins.

Usage: python -m app.reasoner   (port REASONER_PORT, default 8100)
"""
import json
import os
import tempfile
import time
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import liveness, providers

MAX_BODY = 2_000_000          # the worker's envelope is bounded far below this
MAX_TIMEOUT_S = 1800
MAX_TURNS = 20
# No served liveness probe for this long: exit so docker restarts the reasoner (app/liveness.py).
HANG_EXIT_S = float(os.environ.get("REASONER_HANG_EXIT_S", "300"))


def run(local: dict[str, providers.Provider], body: dict) -> tuple[int, dict]:
    """Validate one request and run it. Returns (http status, payload)."""
    prov = local.get(body.get("provider"))
    if prov is None:
        return 400, {"error": f"unknown provider {body.get('provider')!r}"}
    prompt, schema = body.get("prompt"), body.get("schema")
    timeout_s, max_turns = body.get("timeout_s"), body.get("max_turns")
    if not (isinstance(prompt, str) and isinstance(schema, dict)
            and isinstance(timeout_s, int) and 0 < timeout_s <= MAX_TIMEOUT_S
            and isinstance(max_turns, int) and 0 < max_turns <= MAX_TURNS):
        return 400, {"error": "prompt, schema, timeout_s and max_turns are required and bounded"}
    # Same as the worker did before: an empty scratch directory, nothing to read in it.
    with tempfile.TemporaryDirectory(prefix="reasoner-") as scratch:
        result = prov.run(prompt, schema, cwd=scratch, timeout_s=timeout_s, max_turns=max_turns)
    return 200, {**asdict(result), "health": result.health.value}


def health(local: dict[str, providers.Provider]) -> dict:
    out = {}
    for name, prov in local.items():
        h, why = prov.health_check()
        out[name] = {"health": h.value, "why": why,
                     "version": prov.version() if h in providers.RUNNABLE_HEALTH else None}
    return out


def handler(local: dict[str, providers.Provider]):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/live":       # the server loop answers; runs no CLI
                return self._send(200, {"live": True})
            if self.path == "/health":
                return self._send(200, health(local))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/run":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("content-length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._send(413, {"error": "body missing or too large"})
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return self._send(400, {"error": "body is not JSON"})
            self._send(*run(local, body if isinstance(body, dict) else {}))

        def log_message(self, fmt, *args):  # prompts never reach the log; method/path/status do
            print(f"reasoner {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    return Handler


def live_progress(url: str, clock=time.monotonic):
    """Progress = the last time this server answered its own /live (for liveness.watch)."""
    last = [clock()]
    # Never through HTTP(S)_PROXY: the reasoner's egress proxy filters its own loopback address.
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def progress() -> float:
        with direct.open(url.rstrip("/") + "/live", timeout=10) as r:
            if r.status == 200:
                last[0] = clock()
        return last[0]
    return progress


def main() -> None:
    # Local CLIs only: this process must never forward to another reasoner.
    local = providers.default_providers({k: v for k, v in os.environ.items() if k != "REASONER_URL"})
    port = int(os.environ.get("REASONER_PORT", "8100"))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler(local))
    liveness.start(live_progress(f"http://127.0.0.1:{port}"), limit_s=HANG_EXIT_S)
    server.serve_forever()


if __name__ == "__main__":
    main()
