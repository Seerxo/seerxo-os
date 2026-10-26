"""Release review signer (F1): the only holder of the reviewer private key. Runs no model and accepts no verdict.

Why a separate container: in company-reasoner the key would sit beside model CLIs that the worker can drive through
the unauthenticated /run; a read-only sandbox still reads files, so the key could be exfiltrated. Here:

- no model CLI, no npm, no Docker socket, read-only root, all capabilities dropped, its own uid (10002), its own
  internal network shared only with company-reasoner (the worker is not on it), no route to the internet;
- the gateway reaches it through a Unix socket in a host directory no agent container mounts;
- each request must be signed by the gateway key (namespace seerxo-review-request), be fresh, and carry a nonce
  never seen before; only then does the signer build the review itself (fixed RELEASE_REVIEW_PROMPT + the
  packet), ask company-reasoner, validate the structured answer strictly, and sign the attestation.

Nothing a caller sends can become the verdict: the request has no verdict field, and the reasoner's answer is
accepted only if it matches the schema and copies this packet's fingerprint. Standard library only.

Usage (container): python -m app.review_signer
"""
import json
import os
import socketserver
import stat
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from app import release_gateway as rg

MAX_BODY = 4_000_000
REVIEW_TIMEOUT_S = 900
REQUEST_FIELDS = {"packet", "issued_at", "request_signature"}


class Signer:
    def __init__(self, key_path: str, requester_key: str | None, ask, provider: str = "codex", clock=None):
        self.key_path, self.requester_key, self.ask, self.provider = Path(key_path), requester_key, ask, provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.nonces: dict[str, float] = {}
        self.lock = threading.Lock()

    def key_ready(self) -> str | None:
        """Why the key must not be used, or None. Only a private regular file owned by this uid counts."""
        try:
            st = os.lstat(self.key_path)
        except OSError:
            return "review signing key is not installed"
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return "review signing key must be a regular file owned by the signer with mode 0600 or 0400"
        if not self.requester_key:
            return "gateway requester key is not installed"
        return None

    def handle(self, body) -> tuple[int, dict]:
        problem = self.key_ready()
        if problem:
            return 503, {"error": problem}
        if not isinstance(body, dict) or set(body) != REQUEST_FIELDS or not rg.valid_packet(body["packet"]):
            return 400, {"error": "a review request is exactly {packet, issued_at, request_signature}, well-formed"}
        packet, asked_at, now = body["packet"], body["issued_at"], self.clock()
        try:
            age = (now - datetime.fromisoformat(asked_at)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if (age is None or not -rg.CLOCK_SKEW_S <= age <= rg.CLOCK_SKEW_S
                or not rg.verify(rg.review_request(packet, asked_at), body["request_signature"], self.requester_key,
                                 rg.NS_REVIEW_REQUEST)):
            return 403, {"error": "review requests must be signed by the gateway and recent"}
        with self.lock:                                    # check-and-set atomically under ThreadingMixIn
            stamp = now.timestamp()
            for old in [n for n, at in self.nonces.items() if stamp - at > 2 * rg.CLOCK_SKEW_S]:
                del self.nonces[old]
            if packet["nonce"] in self.nonces:
                return 409, {"error": "nonce already used"}
            self.nonces[packet["nonce"]] = stamp
        try:
            result = self.ask({"provider": self.provider, "prompt": rg.RELEASE_REVIEW_PROMPT + json.dumps(packet),
                               "schema": rg.RELEASE_REVIEW_SCHEMA, "timeout_s": REVIEW_TIMEOUT_S, "max_turns": 1})
        except Exception as error:   # the reviewer is unreachable: no verdict, nothing signed
            return 502, {"error": f"reviewer unreachable: {type(error).__name__}"}
        # company-reasoner /run returns asdict(ProviderResult) + health, which has no "ok" key (ok is a property);
        # derive it exactly as ProviderResult.ok does (app/providers.py, keep in step): AVAILABLE and output present.
        ok = isinstance(result, dict) and result.get("health") == "AVAILABLE" and result.get("output") is not None
        output = result.get("output") if ok else None
        if not rg.valid_review_output(output, packet):
            return 502, {"error": "reviewer gave no valid verdict for this packet; nothing signed"}
        att = rg.attestation(self.provider, packet, output, self.clock().isoformat())
        return 200, {"attestation": att, "signature": rg.sign(rg.canonical(att), self.key_path, rg.NS_REVIEW)}


def reasoner_ask(url: str):
    def ask(body: dict) -> dict:
        req = urllib.request.Request(url.rstrip("/") + "/run", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=REVIEW_TIMEOUT_S + 60) as resp:
            return json.loads(resp.read())
    return ask


def handler(signer: Signer):
    class Handler(BaseHTTPRequestHandler):
        def address_string(self):           # Unix socket peers have no host/port
            return "unix"

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/review":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("content-length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._send(413, {"error": "body missing or too large"})
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                return self._send(400, {"error": "body is not JSON"})
            self._send(*signer.handle(body))

        def log_message(self, fmt, *args):   # never the packet, never a key path
            print(f"review-signer {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    return Handler


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(socket_path: str, signer: Signer) -> UnixHTTPServer:
    path = Path(socket_path)
    if path.exists() or path.is_symlink():
        path.unlink()
    server = UnixHTTPServer(str(path), handler(signer))
    os.chmod(path, 0o660)            # the gateway user reaches it through the shared group; others cannot
    return server


def main() -> int:
    key, requester = os.environ.get("REVIEW_SIGNING_KEY", ""), os.environ.get("REVIEW_REQUESTER_KEY", "")
    requester_key = Path(requester).read_text().strip() if requester and Path(requester).is_file() else None
    signer = Signer(key, requester_key, reasoner_ask(os.environ["REASONER_URL"]))
    if signer.key_ready():
        print(f"review-signer: {signer.key_ready()}; requests will be refused", file=sys.stderr)
    serve(os.environ["REVIEW_SIGNER_SOCKET"], signer).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
