"""One Stage C worker process for the crash harness (tests/test_stage_c_crash_recovery_postgres.py).

Same local fixture as tests/test_stage_c_worker.py::pipeline (a fixture mirror, a bare local remote, a fake
coder, local checks, a fake PR opener, a deterministic reviewer), but idempotent: the mirror and remote are
created once by the seed step and reused by every later process, so a restart sees the same repository.

Kill points (env KILL_AT): during_coder_start, after_coder_start, during_coder_run, before_publish, during_open_pr,
during_repair_coder_run (the second coder run, a CI repair of the published PR; needs CI_FAILED=1). Each one
SIGKILLs this process at that point, with no cleanup. CALLS_LOG records every coder run and every PR opening
(one line each, fsynced), so counts survive the kill.
"""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import control, enums, models, providers, repo_guardian_github, worker, stage_c_ci, stage_c_worker, stage_c_runner

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(os.environ["FIXTURE_DIR"])
MIRROR = FIXTURE / "jobs" / ".control" / "sez-company.git"
REMOTE = FIXTURE / "remote.git"
KILL_AT = os.environ.get("KILL_AT", "")
CALLS_LOG = os.environ["CALLS_LOG"]
REPO = "https://github.com/seerxo/seerxo-os.git"


def _log(line: str) -> None:
    with open(CALLS_LOG, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _kill(point: str) -> None:
    if KILL_AT == point:
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)


class _Patch:
    """The subset of pytest's monkeypatch that the fixture needs. Patches live only in this process."""

    def setattr(self, obj, name, value, raising=True):
        setattr(obj, name, value)

    def setenv(self, name, value):
        os.environ[name] = value


def ensure_fixture() -> None:
    """Create the mirror and the bare remote exactly once; later processes reuse them unchanged."""
    if MIRROR.exists():
        return
    MIRROR.mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(MIRROR), *a], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (MIRROR / "tests").mkdir()
    (MIRROR / "tests/test_metrics.py").write_text("def test_metric():\n    assert 1 == 0\n")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
    subprocess.run(["git", "init", "--bare", "-q", str(REMOTE)], check=True)


def install_fixture(p: _Patch) -> None:
    p.setenv("STAGE_C_JOBS_ROOT", str(FIXTURE / "jobs"))
    p.setattr(worker, "ENABLE_AUTONOMOUS_CODE_WRITE", os.environ.get("WRITES_DISABLED") != "1")
    p.setattr(stage_c_worker, "ready", lambda db: None, raising=False)
    p.setattr(stage_c_worker, "runtime_ready", lambda db, now=None: True)
    p.setattr(stage_c_worker, "publication_role", lambda: ("PASS", "read-only (test)"))
    p.setattr(control, "pilot_authorization_denial", lambda *a, **k: None)
    p.setattr(control, "consume_pilot_authorization", lambda *a, **k: None)
    # Same-repository publishing, as in tests/test_stage_c_worker.py (the committed policy names the B5 fork).
    load = repo_guardian_github.load_policy
    p.setattr(repo_guardian_github, "load_policy",
              lambda path: {k: v for k, v in load(path).items() if k != "publish_forks"})
    original_git = stage_c_runner._git

    def local_git(repo, *args, **kw):
        if args[0] == "fetch":
            return ""  # external fetch boundary; the fixture mirror already has main
        if args[:2] == ("ls-remote", REPO):
            args = (args[0], str(REMOTE), *args[2:])
            kw["protocol"] = "file"
        return original_git(repo, *args, **kw)
    p.setattr(stage_c_runner, "_git", local_git)

    def coder(directory, job):
        earlier = sum(1 for ln in Path(CALLS_LOG).read_text().splitlines() if ln.startswith("coder ")) \
            if Path(CALLS_LOG).exists() else 0
        _log(f"coder {job.task_key}")
        if earlier:
            (directory / job.paths[0]).write_text("def test_metric():\n    assert 2 == 2\n")
            _kill("during_repair_coder_run")  # the repair's coder wrote its change and has not returned
        (directory / job.paths[0]).write_text("def test_metric():\n    assert 1 == 1\n")
        _kill("during_coder_run")  # the coder has written its change but has not returned

    def launcher_coder(**kw):
        def call(directory, job):
            before, after = kw.get("before_enqueue"), kw.get("after_start")
            if before is not None:
                before()
            try:
                _kill("during_coder_start")
                if after is not None:
                    after(True)
                    after = None
                _kill("after_coder_start")
                return coder(directory, job)
            finally:
                if after is not None:
                    after(False)
        return call
    p.setattr(stage_c_runner, "launcher_coder", launcher_coder)

    def checks(directory):
        done = directory.parent / "done"
        done.mkdir(exist_ok=True)
        (done / "result.json").write_text('{"exit": 0}')
        result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=directory, capture_output=True)
        return {"passed": result.returncode == 0, "summary": "local fixture checks"}
    p.setattr(stage_c_runner, "launcher_checks", lambda repo, **kw: checks)

    if os.environ.get("CI_FAILED") == "1":
        from tests.test_stage_c_worker import GREEN_STATUSES

        def failed_ci(repository, number, *a, **k):
            # The published PR is open and its required check failed on the exact pushed head.
            head = subprocess.run(["git", "--git-dir", str(REMOTE), "rev-parse", "refs/heads/task/TASK-0100"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            return {"state": "open", "head_sha": head, "checks_complete": True, "statuses": dict(GREEN_STATUSES),
                    "statuses_complete": True, "status_problems": [],
                    "checks": [{"name": "ci", "status": "completed", "conclusion": "failure"}]}
        p.setattr(stage_c_ci, "gh_facts", failed_ci)

    original_publish = stage_c_runner.publish

    def publish(db, task, job, diff, worktree, destination, open_pr, *args, **kwargs):
        assert destination == REPO
        _kill("before_publish")

        def fake_open_pr(branch, title, body, commit):
            url = f"https://github.com/{stage_c_worker.REPOSITORY}/pull/999"
            with (FIXTURE / "published-pr.json").open("w") as f:
                json.dump([{"number": 999, "state": "open", "html_url": url,
                            "base": {"ref": "main", "repo": {"full_name": stage_c_worker.REPOSITORY}},
                            "head": {"ref": branch, "sha": commit,
                                     "repo": {"full_name": stage_c_worker.REPOSITORY}}}], f)
                f.flush()
                os.fsync(f.fileno())
            _log(f"pr_open {task.key}")
            _kill("during_open_pr")
            if os.environ.get("LOSE_PR_RESPONSE") == "1":
                raise TimeoutError("publication response and verification unavailable")
            return url
        return original_publish(db, task, job, diff, worktree, str(REMOTE), fake_open_pr, *args, **kwargs)
    p.setattr(stage_c_runner, "publish", publish)
    p.setattr(repo_guardian_github, "api", lambda *args: json.loads((FIXTURE / "published-pr.json").read_text()))


def main() -> None:
    mode = sys.argv[1]  # "run" or "seed"
    ensure_fixture()
    from tests.test_stage_c_worker import Reviewer, write_task
    from app import registry
    class LoggedReviewer(Reviewer):
        def health_check(self):
            if os.environ.get("REVIEWERS_DOWN") == "1":
                return providers.Health.QUOTA_EXHAUSTED, None
            return super().health_check()

        def _run(self, prompt, schema, **kwargs):
            if schema == stage_c_runner.REVIEW_SCHEMA:
                _log(f"review {self.name}")
            return super()._run(prompt, schema, **kwargs)

    install_fixture(_Patch())
    engine = create_engine(os.environ["DATABASE_URL"], future=True)
    with Session(engine) as db:
        if mode == "seed":
            # create_all already seeds the default agents on this database; add only what is missing.
            for name in ("ceo", "developer", "supervisor", "research"):
                if db.query(models.Agent).filter_by(key=name).first() is None:
                    db.add(models.Agent(key=name, role=name, enabled=True))
            db.commit()
            task = write_task(db)
            task.context = {**task.context, "improvement": {"measure": {
                "selector": {"subject": "repository:seerxo/seerxo-os", "metric": "metrics_test_cases",
                             "source": "pytest-collection", "window": ""}, "direction": "up", "window_hours": 0}}}
            db.commit()
            return
        reviewers = {n: LoggedReviewer(n) for n in ("claude", "codex")}
        out = worker.run_once(db, reviewers, registry.load())
    print(json.dumps({"out": out, "stage": None}, default=str))


if __name__ == "__main__":
    main()
