"""One worker process for the crash-recovery harness (tests/test_worker_crash_recovery_postgres.py).

Runs worker.run_once against the disposable PostgreSQL named by DATABASE_URL, with a fake provider that
records every model call to disk before returning. CRASH_AT=k with CRASH_PHASE=commit_before|commit_after|
provider_return makes this process SIGKILL itself at the k-th Session.commit (or right after the provider
returns, before anything is persisted). SIGKILL runs no finally blocks, no atexit, no rollback: the open
transaction is rolled back only by PostgreSQL when the connection drops, which is the real crash path.
"""
import json
import os
import signal
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import registry, worker
from app.providers import Health, Provider, ProviderResult

CRASH_AT = int(os.environ.get("CRASH_AT", "0"))
CRASH_PHASE = os.environ.get("CRASH_PHASE", "commit_before")
CALLS_FILE = os.environ["CALLS_FILE"]
OUTPUTS_FILE = os.environ["OUTPUTS_FILE"]
RESULT_FILE = os.environ["RESULT_FILE"]
PROVIDER = os.environ.get("PROVIDER", "claude")


def _kill() -> None:
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def _append_call(line: str) -> None:
    with open(CALLS_FILE, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


class FileProvider(Provider):
    """Deterministic stand-in for a model CLI: outputs come from a file, call count from disk."""

    def __init__(self, name: str):
        super().__init__(name=name, binary=name, environ={"PATH": ""})

    def health_check(self):
        return Health.AVAILABLE, None

    def _run(self, prompt, schema, **kw):
        n = (sum(1 for _ in open(CALLS_FILE)) if os.path.exists(CALLS_FILE) else 0) + 1
        _append_call(f"call {n} {self.name}")
        outputs = json.load(open(OUTPUTS_FILE))
        out = outputs[min(n, len(outputs)) - 1]
        if CRASH_PHASE == "provider_return" and n == CRASH_AT:
            _kill()
        return ProviderResult(self.name, Health.AVAILABLE, output=out, model="fake")


def main() -> None:
    engine = create_engine(os.environ["DATABASE_URL"], future=True)
    commits = {"n": 0}
    original_commit = Session.commit

    def counted_commit(self, *args, **kwargs):
        commits["n"] += 1
        here = commits["n"] == CRASH_AT
        if here and CRASH_PHASE == "commit_before":
            _kill()
        result = original_commit(self, *args, **kwargs)
        if here and CRASH_PHASE == "commit_after":
            _kill()
        return result

    Session.commit = counted_commit
    with Session(engine) as db:
        out = worker.run_once(db, {PROVIDER: FileProvider(PROVIDER)}, registry.load())
    with open(RESULT_FILE, "w") as f:
        json.dump({"out": out, "commits": commits["n"]}, f, default=str)


if __name__ == "__main__":
    main()
