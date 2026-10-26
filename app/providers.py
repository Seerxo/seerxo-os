"""Model provider adapters: the only place that knows Claude Code or Codex CLI syntax.

Company logic asks a provider to `run` a task envelope and gets back a ProviderResult.
Nothing outside this module builds a CLI command line, so a CLI change is a one-file fix
and a new provider is one more class (docs/AUTONOMOUS_AGENT_ARCHITECTURE.md §4).

Zero-cost mode (CLAUDE.md §26A) is enforced here, not trusted: a provider that would bill
a metered API key reports METERED_BLOCKED and never starts. Subscription logins
(`CLAUDE_CODE_OAUTH_TOKEN`, Codex `auth_mode = chatgpt`) are existing access; API keys
are new spend. A quota error is never answered by buying capacity.
"""
import enum
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Any of these in the worker's environment means a run could bill per token.
METERED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "OPENAI_API_KEY", "CODEX_API_KEY",
)
# Only these reach the child process; everything else (including metered keys that
# slipped in) is dropped. Defence in depth on top of the METERED_BLOCKED refusal.
# USER/LOGNAME/TMPDIR: the macOS keychain login is invisible without them.
# HTTPS_PROXY/HTTP_PROXY/NO_PROXY: in company-reasoner the only route out is the proxy.
PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "TZ",
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")


class Health(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    CREDENTIALS_CONFIGURED = "CREDENTIALS_CONFIGURED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    BROKEN = "BROKEN"
    DISABLED = "DISABLED"
    METERED_BLOCKED = "METERED_BLOCKED"


# Worth trying the next provider for; anything else is the task's problem, not capacity.
FALLBACK_HEALTH = {Health.AUTH_REQUIRED, Health.RATE_LIMITED, Health.QUOTA_EXHAUSTED,
                   Health.DISABLED, Health.BROKEN}


@dataclass
class ProviderResult:
    provider: str
    health: Health
    output: dict | None = None
    error: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # What the run would cost at list price. Under a subscription it is not billed;
    # recorded so the founder can see usage, never used to decide spend.
    list_cost_usd: float | None = None
    duration_s: float = 0.0
    exit_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.health == Health.AVAILABLE and self.output is not None


RUNNABLE_HEALTH = {Health.AVAILABLE, Health.CREDENTIALS_CONFIGURED}


# (argv, cwd, timeout_s, env) -> (exit_code or None on timeout, stdout, stderr)
Runner = Callable[..., tuple[int | None, str, str]]   # (argv, cwd, timeout, env, stdin=None)


def subprocess_runner(argv: list[str], cwd: str, timeout: int, env: dict,
                      stdin: str | None = None) -> tuple[int | None, str, str]:
    # stdin closed unless text is given: codex otherwise waits on "Reading additional input from stdin".
    # New session so a timeout kills the whole process group, not just the CLI wrapper.
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)
        out, err = proc.communicate()
        return None, out or "", err or ""


def classify(message: str) -> Health:
    m = (message or "").lower()
    if re.search(r"usage limit|session limit|quota|credit|insufficient|billing|payment", m):
        return Health.QUOTA_EXHAUSTED
    if re.search(r"rate.?limit|\b429\b|overloaded|too many requests", m):
        return Health.RATE_LIMITED
    # "Your organization has disabled Claude subscription access for Claude Code" (2026-10-23): the account cannot
    # run at all, so it is a login problem, never "available but this run failed".
    if re.search(r"log ?in|logged in|unauthori[sz]ed|\b401\b|invalid api key|authenticat|oauth|organization has disabled .*subscription access", m):
        return Health.AUTH_REQUIRED
    return Health.BROKEN


_RESET = re.compile(r"try again (?:at|in) ([^.)]+)", re.I)


def quota_message(raw: str) -> str:
    """Subscription limits mean wait. Provider text that suggests buying usage is not
    stored or shown anywhere (Desk, Telegram, task history)."""
    reset = _RESET.search(raw or "")
    return "subscription usage limit reached; waiting for reset" + (
        f" (provider says: try again {reset.group(0)[10:].strip()})" if reset else "")


def metered_env(environ: dict) -> list[str]:
    return [k for k in METERED_ENV if environ.get(k)]


def child_env(environ: dict) -> dict:
    env = {k: environ[k] for k in PASS_ENV if environ.get(k)}
    # Observed 2026-10-01: Claude Code also calls a telemetry intake. Not needed for the
    # work, so it is switched off: one fewer egress destination to allow (§6).
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


@dataclass
class Provider:
    name: str
    binary: str
    model: str | None = None
    environ: dict = field(default_factory=lambda: dict(os.environ))
    runner: Runner = subprocess_runner

    def health_check(self) -> tuple[Health, str | None]:
        found = metered_env(self.environ)
        if found:
            return Health.METERED_BLOCKED, f"metered credentials present: {', '.join(found)}"
        if not shutil.which(self.binary, path=self.environ.get("PATH")):
            return Health.DISABLED, f"{self.binary} is not installed"
        return self._auth_check()

    def _auth_check(self) -> tuple[Health, str | None]:
        return Health.AVAILABLE, None

    def version(self) -> str | None:
        """CLI version for run provenance. Cached; never fatal."""
        if not hasattr(self, "_version"):
            try:
                code, out, _ = self.runner([self.binary, "--version"], "/", 10, child_env(self.environ))
                self._version = out.strip().splitlines()[0] if code == 0 and out.strip() else None
            except (OSError, IndexError):
                self._version = None
        return self._version

    def run(self, prompt: str, schema: dict, *, cwd: str, timeout_s: int,
            max_turns: int) -> ProviderResult:
        health, why = self.health_check()
        if health not in RUNNABLE_HEALTH:
            return ProviderResult(self.name, health, error=why)
        started = time.monotonic()
        result = self._run(prompt, schema, cwd=cwd, timeout_s=timeout_s, max_turns=max_turns)
        result.duration_s = round(time.monotonic() - started, 2)
        return result

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns) -> ProviderResult:
        raise NotImplementedError


class ClaudeCodeProvider(Provider):
    """`claude -p` with structured output. No tools: the first slice only reasons over
    the context it is given, so there is nothing to grant (Stage A/B rollout)."""

    def __init__(self, model: str | None = "sonnet", **kw):
        super().__init__(name="claude", binary="claude", model=model, **kw)

    def _auth_check(self):
        # macOS keeps the login in the keychain, which cannot be inspected cheaply; on the
        # server the subscription token or a credentials file must exist.
        home = Path(self.environ.get("CLAUDE_CONFIG_DIR") or Path(self.environ.get("HOME", "~")) / ".claude")
        if (self.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or self.environ.get("CLAUDE_CODE_OAUTH_TOKEN_2")
                or sys.platform == "darwin"
                or (home.expanduser() / ".credentials.json").exists()):
            return Health.CREDENTIALS_CONFIGURED, "Claude credentials found; live model access is unverified"
        return Health.AUTH_REQUIRED, "claude is not logged in (set CLAUDE_CODE_OAUTH_TOKEN)"

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns):
        """One run, on the first subscription account that can answer. The second token
        (CLAUDE_CODE_OAUTH_TOKEN_2, founder decision 2026-10-23: two Claude accounts) is tried only when the first
        produced no answer because of login, quota or rate limit; an account that answered is never re-asked."""
        tokens = [self.environ[v] for v in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_2") if self.environ.get(v)]
        res = None
        for token in tokens or [None]:
            env = {k: v for k, v in self.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN_2"}
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            res = self._run_once(prompt, schema, cwd=cwd, timeout_s=timeout_s, max_turns=max_turns, environ=env)
            if res.output is not None or res.health not in (Health.AUTH_REQUIRED, Health.QUOTA_EXHAUSTED,
                                                            Health.RATE_LIMITED):
                return res
        return res

    def _run_once(self, prompt, schema, *, cwd, timeout_s, max_turns, environ):
        # The prompt goes on stdin: one argv string is capped at 128 KiB on Linux, and a release review packet
        # is larger ("Argument list too long", gateway REVIEW_UNKNOWN on 7a8a82b, 2026-10-23).
        argv = [self.binary, "-p", "--output-format", "json",
                "--json-schema", json.dumps(schema), "--tools", "",
                "--permission-mode", "dontAsk", "--max-turns", str(max_turns),
                "--no-session-persistence"]
        if self.model:
            argv += ["--model", self.model]
        code, out, err = self.runner(argv, cwd, timeout_s, child_env(environ), prompt)
        if code is None:
            return ProviderResult(self.name, Health.AVAILABLE, error=f"timeout after {timeout_s}s")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            msg = (err or out or f"exit {code}").strip()[:500]
            health = classify(msg)
            return ProviderResult(self.name, health, exit_code=code,
                                  error=quota_message(msg) if health == Health.QUOTA_EXHAUSTED else msg)
        usage = data.get("usage") or {}
        res = ProviderResult(
            self.name, Health.AVAILABLE, exit_code=code,
            model=next(iter(data.get("modelUsage") or {}), self.model),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            list_cost_usd=data.get("total_cost_usd"),
        )
        if data.get("is_error") or code != 0:
            res.error = str(data.get("result") or data.get("subtype") or f"exit {code}")[:500]
            res.health = classify(res.error)
            if res.health == Health.QUOTA_EXHAUSTED:
                res.error = quota_message(res.error)
            if res.health == Health.BROKEN:
                res.health = Health.AVAILABLE  # the provider works; this run failed
            return res
        out_obj = data.get("structured_output")
        if not isinstance(out_obj, dict):
            res.error = "no structured_output in result"
        res.output = out_obj if isinstance(out_obj, dict) else None
        return res


class CodexProvider(Provider):
    """`codex exec` in the read-only sandbox, ephemeral, with an output schema."""

    def __init__(self, model: str | None = None, **kw):
        super().__init__(name="codex", binary="codex", model=model, **kw)

    def _auth_check(self):
        home = Path(self.environ.get("CODEX_HOME") or Path(self.environ.get("HOME", "~")) / ".codex")
        try:
            auth = json.loads((home.expanduser() / "auth.json").read_text())
        except (OSError, json.JSONDecodeError):
            return Health.AUTH_REQUIRED, "codex is not logged in (no auth.json)"
        # Only the mode and key presence are read; token values never leave the file.
        if auth.get("OPENAI_API_KEY") or auth.get("auth_mode") not in ("chatgpt", None):
            return Health.METERED_BLOCKED, f"codex auth_mode={auth.get('auth_mode')} bills per token"
        # auth.json proves only that subscription credentials are configured. Expiry,
        # entitlement and model reachability require an explicit provider run.
        return Health.CREDENTIALS_CONFIGURED, "ChatGPT credentials found; live model access is unverified"

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns):
        with tempfile.TemporaryDirectory(prefix="codex-run-") as tmp:
            schema_path, last_path = Path(tmp) / "schema.json", Path(tmp) / "last.json"
            schema_path.write_text(json.dumps(schema))
            argv = [self.binary, "exec", "--sandbox", "read-only", "--ephemeral",
                    "--skip-git-repo-check", "--json", "-C", cwd,
                    "--output-schema", str(schema_path), "-o", str(last_path)]
            if self.model:
                argv += ["-m", self.model]
            # "-": the prompt is read from stdin. One argv string is capped at 128 KiB on Linux and a release
            # review packet is larger (B-14, 2026-10-23); see ClaudeCodeProvider.
            argv.append("-")
            code, out, err = self.runner(argv, cwd, timeout_s, child_env(self.environ), prompt)
            if code is None:
                return ProviderResult(self.name, Health.AVAILABLE, error=f"timeout after {timeout_s}s")
            res = ProviderResult(self.name, Health.AVAILABLE, exit_code=code, model=self.model)
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") in ("error", "turn.failed"):
                    res.error = str(ev.get("message") or (ev.get("error") or {}).get("message"))[:500]
                if ev.get("type") == "turn.completed":
                    usage = ev.get("usage") or {}
                    res.input_tokens = usage.get("input_tokens")
                    res.output_tokens = usage.get("output_tokens")
            if res.error or code != 0:
                res.error = res.error or (err.strip()[-500:] or f"exit {code}")
                health = classify(res.error)
                if health == Health.QUOTA_EXHAUSTED:
                    res.error = quota_message(res.error)
                res.health = Health.AVAILABLE if health == Health.BROKEN else health
                return res
            try:
                res.output = json.loads(last_path.read_text())
            except (OSError, json.JSONDecodeError):
                res.error = "codex returned no parseable final message"
            return res


class RemoteProvider(Provider):
    """A provider whose CLI runs in company-reasoner (app/reasoner.py), not in this process.
    Same contract as a local one; an unreachable reasoner is BROKEN, so the worker falls
    back or waits instead of failing the task."""

    def __init__(self, name: str, url: str, client=None, **kw):
        super().__init__(name=name, binary=name, **kw)
        self.url = url.rstrip("/")
        self.client = client

    def _http(self):
        import httpx  # the worker image has it; tests pass a client
        return self.client or httpx.Client()

    def _remote_health(self) -> dict:
        try:
            r = self._http().get(f"{self.url}/health", timeout=15)
            r.raise_for_status()
            return r.json().get(self.name) or {"health": "DISABLED", "why": "reasoner has no such provider"}
        except Exception as e:  # noqa: BLE001 — any transport failure means "not reachable"
            return {"health": "BROKEN", "why": f"reasoner unreachable: {type(e).__name__}"}

    def health_check(self) -> tuple[Health, str | None]:
        found = metered_env(self.environ)  # still refused here, before any request leaves
        if found:
            return Health.METERED_BLOCKED, f"metered credentials present: {', '.join(found)}"
        h = self._remote_health()
        return Health(h["health"]), h.get("why")

    def version(self) -> str | None:
        return self._remote_health().get("version")

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns) -> ProviderResult:
        body = {"provider": self.name, "prompt": prompt, "schema": schema,
                "timeout_s": timeout_s, "max_turns": max_turns}
        try:
            r = self._http().post(f"{self.url}/run", json=body, timeout=timeout_s + 60)
            r.raise_for_status()
            data = r.json()
            return ProviderResult(**{**data, "health": Health(data["health"])})
        except Exception as e:  # noqa: BLE001
            return ProviderResult(self.name, Health.BROKEN, error=f"reasoner: {type(e).__name__}: {str(e)[:300]}")


def default_providers(environ: dict | None = None) -> dict[str, Provider]:
    env = dict(os.environ if environ is None else environ)
    if env.get("REASONER_URL"):  # production: the CLIs live in company-reasoner
        return {n: RemoteProvider(n, env["REASONER_URL"], environ=env) for n in ("claude", "codex")}
    return {
        "claude": ClaudeCodeProvider(model=env.get("WORKER_CLAUDE_MODEL", "sonnet"), environ=env),
        "codex": CodexProvider(model=env.get("WORKER_CODEX_MODEL") or None, environ=env),
    }
