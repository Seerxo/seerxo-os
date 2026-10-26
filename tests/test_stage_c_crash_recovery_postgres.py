"""Stage C crash recovery across real SIGKILLs, on a disposable PostgreSQL.

The child process (tests/stage_c_crash_child.py) runs the same local Stage C fixture as the in-process tests.
A kill point SIGKILLs the worker at a Stage C boundary. A second process then runs recovery after a modelled
restart. Invariants: no duplicate pull request, no duplicate coder run, one branch on the remote, and the task
never reaches PR_OPEN without a recorded pull request.

Requires EXTERNAL_RELEASE_TEST_DATABASE_URL pointing at company_external_release_test.
"""
import os
import pathlib
import signal
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import models

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "stage_c_crash_child.py"
URL = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="disposable PostgreSQL URL not supplied")
POINTS = ["during_coder_start", "after_coder_start", "during_coder_run", "before_publish", "during_open_pr"]
# (status, stage, coder runs, pull requests opened) after recovery, per kill point.
EXPECTED = {
    "during_coder_start": ("CANCELLED", "ABANDONED", 0, 0),
    "after_coder_start": ("CANCELLED", "ABANDONED", 0, 0),
    "during_coder_run": ("CANCELLED", "ABANDONED", 1, 0),  # the change was written, never reviewed or published
    "before_publish": ("REVIEW", "PR_OPEN", 1, 1),       # approved diff published without re-running the coder
    "during_open_pr": ("REVIEW", "PR_OPEN", 1, 1),  # discover the exact existing PR; never create another
}


def _schema_url(schema):
    """The disposable database, with search_path pinned to one schema. Concurrent runs cannot collide."""
    return make_url(URL).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)


def _new_schema(fixture):
    assert make_url(URL).database == "company_external_release_test", "disposable database only"
    schema = f"stagec_{uuid.uuid4().hex[:12]}"
    base = create_engine(URL, future=True)
    with base.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    base.dispose()
    (fixture / "db_url").write_text(_schema_url(schema))
    return schema


def _drop_schema(schema):
    base = create_engine(URL, future=True)
    with base.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    base.dispose()


def _reset(engine, fixture):
    models.Base.metadata.create_all(engine)
    _child(fixture, "seed")


def _child(fixture, mode, kill_at="", *, reviewers_down=False, lose_pr_response=False, writes_disabled=False,
           ci_failed=False):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "DATABASE_URL": (fixture / "db_url").read_text(),
           "FIXTURE_DIR": str(fixture), "CALLS_LOG": str(fixture / "calls.log"), "KILL_AT": kill_at,
           "REVIEWERS_DOWN": "1" if reviewers_down else "0",
           "LOSE_PR_RESPONSE": "1" if lose_pr_response else "0",
           "WRITES_DISABLED": "1" if writes_disabled else "0",
           "CI_FAILED": "1" if ci_failed else "0"}
    return subprocess.run([sys.executable, str(CHILD), mode], env=env, cwd=ROOT, capture_output=True,
                          text=True, timeout=300)


def _restart(engine):
    past = timedelta(days=3)
    with Session(engine) as db:
        for t in db.execute(select(models.Task)).scalars():
            t.updated_at = t.updated_at - past
            if t.started_at:
                t.started_at = t.started_at - past
        for r in db.execute(select(models.AgentRun)).scalars():
            r.started_at = r.started_at - past
        db.commit()


def _lines(fixture, prefix):
    log = fixture / "calls.log"
    return sum(1 for ln in log.read_text().splitlines() if ln.startswith(prefix)) if log.exists() else 0


def _branches(fixture):
    out = subprocess.run(["git", "--git-dir", str(fixture / "remote.git"), "for-each-ref", "refs/heads/task/"],
                         capture_output=True, text=True, check=True).stdout
    return [ln.split()[-1] for ln in out.splitlines()]


def _task_state(engine):
    with Session(engine) as db:
        t = db.execute(select(models.Task)).scalar_one()
        stage = (t.context or {}).get("improvement", {}).get("stage")
        return {"status": t.status.value, "stage": stage}


@pytest.mark.parametrize("point,reviewers_down", [(p, False) for p in POINTS] +
                         [("before_publish", True), ("during_open_pr", True)])
def test_kill_at_stage_c_boundary_then_recovery(tmp_path, point, reviewers_down):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    schema = _new_schema(fixture)
    try:
        engine = create_engine((fixture / "db_url").read_text(), future=True)
        _reset(engine, fixture)
        killed = _child(fixture, "run", kill_at=point)
        assert killed.returncode == -signal.SIGKILL, (killed.returncode, killed.stderr[-600:])
        at_kill = _task_state(engine)
        _restart(engine)
        recovery = _child(fixture, "run", reviewers_down=reviewers_down)
        assert recovery.returncode == 0, recovery.stderr[-800:]
        after = _task_state(engine)
    finally:
        _drop_schema(schema)
    row = {"point": point, "at_kill": at_kill, "after": after, "coder_runs": _lines(fixture, "coder"),
           "pr_opens": _lines(fixture, "pr_open"), "review_calls": _lines(fixture, "review"),
           "branches": _branches(fixture)}
    print("\nROW", row)
    # Generic invariants, for every kill point.
    assert after["status"] != "IN_PROGRESS", "task stranded in IN_PROGRESS"
    assert len(row["branches"]) <= 1, "more than one task branch on the remote"
    assert row["pr_opens"] <= 1, "a pull request was opened twice"
    assert row["coder_runs"] <= 1, "the coder ran twice for one attempt"
    assert row["review_calls"] == (1 if point in ("before_publish", "during_open_pr") else 0), row
    # Per-point outcome: the current, test-pinned policy (see tests/test_stage_c_worker.py::
    # test_interrupted_attempt_before_review_is_abandoned_not_recoded: a coder-stage interruption is ABANDONED and
    # CANCELLED, with no retry spent).
    expected = EXPECTED[point]
    assert (after["status"], after["stage"], row["coder_runs"], row["pr_opens"]) == expected, row


def test_ambiguous_pr_publication_stays_blocked_even_if_the_remote_branch_disappears(tmp_path):
    """A PR response alone cannot prove the exact remote commit. Removing the remote
    branch leaves recovery BLOCKED, without a second publication attempt."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    schema = _new_schema(fixture)
    try:
        engine = create_engine((fixture / "db_url").read_text(), future=True)
        _reset(engine, fixture)
        killed = _child(fixture, "run", kill_at="during_open_pr")
        assert killed.returncode == -signal.SIGKILL, killed.stderr[-400:]
        subprocess.run(["git", "--git-dir", str(fixture / "remote.git"), "update-ref", "-d", "refs/heads/task/TASK-0100"],
                       check=True)
        _restart(engine)
        assert _child(fixture, "run").returncode == 0
        assert _task_state(engine)["status"] == "BLOCKED"
        assert _lines(fixture, "pr_open") == 1, "a second pull request was attempted"
    finally:
        _drop_schema(schema)


@pytest.mark.parametrize("missing_remote", [False, True])
def test_terminal_publication_failure_is_observed_after_a_read_only_restart(tmp_path, missing_remote):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    schema = _new_schema(fixture)
    try:
        engine = create_engine((fixture / "db_url").read_text(), future=True)
        _reset(engine, fixture)
        failed = _child(fixture, "run", lose_pr_response=True)
        assert failed.returncode == 0, failed.stderr[-800:]
        assert _task_state(engine) == {"status": "BLOCKED", "stage": "INDEPENDENT_REVIEW"}
        if missing_remote:
            subprocess.run(["git", "--git-dir", str(fixture / "remote.git"), "update-ref", "-d",
                            "refs/heads/task/TASK-0100"], check=True)
        restarted = _child(fixture, "run", reviewers_down=True, writes_disabled=True)
        assert restarted.returncode == 0, restarted.stderr[-800:]
        after = _task_state(engine)
        assert after == ({"status": "BLOCKED", "stage": "INDEPENDENT_REVIEW"} if missing_remote else
                         {"status": "REVIEW", "stage": "PR_OPEN"}), after
        with Session(engine) as db:
            run = db.execute(select(models.AgentRun)).scalar_one()
            assert run.status.value == "FAILED" and run.tool_calls["publication_recovery"]["attempts"] == 1
        assert (_lines(fixture, "coder"), _lines(fixture, "review"), _lines(fixture, "pr_open")) == (1, 1, 1)
    finally:
        _drop_schema(schema)


def test_kill_during_a_ci_repair_keeps_the_pr_tracked_and_never_recodes(tmp_path):
    """Published PR, CI failed, the repair's coder is SIGKILLed mid-run, the worker restarts. Before the fix the
    task was ABANDONED and CANCELLED and its open PR was never reconciled again."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    schema = _new_schema(fixture)
    try:
        engine = create_engine((fixture / "db_url").read_text(), future=True)
        _reset(engine, fixture)
        published = _child(fixture, "run")
        assert published.returncode == 0, published.stderr[-600:]
        assert _task_state(engine) == {"status": "REVIEW", "stage": "PR_OPEN"}
        killed = _child(fixture, "run", kill_at="during_repair_coder_run", ci_failed=True)
        assert killed.returncode == -signal.SIGKILL, (killed.returncode, killed.stderr[-600:])
        assert _task_state(engine) == {"status": "IN_PROGRESS", "stage": "IMPLEMENTING"}
        _restart(engine)
        for _ in range(2):                                   # recovery, then a further loop: still nothing re-runs
            recovery = _child(fixture, "run", ci_failed=True)
            assert recovery.returncode == 0, recovery.stderr[-800:]
        after = _task_state(engine)
        with Session(engine) as db:
            task = db.execute(select(models.Task)).scalar_one()
            error, pr = task.error or "", (task.artifacts or {}).get("pr")
    finally:
        _drop_schema(schema)
    row = {"after": after, "coder_runs": _lines(fixture, "coder"), "pr_opens": _lines(fixture, "pr_open"),
           "review_calls": _lines(fixture, "review"), "branches": _branches(fixture)}
    print("\nROW", row)
    assert after == {"status": "BLOCKED", "stage": "IMPLEMENTING"}, row
    assert error.startswith("REPAIR_INTERRUPTED:") and pr and pr in error, error
    assert row["coder_runs"] == 2, row          # the first attempt and the killed repair; no automatic re-code
    assert row["pr_opens"] == 1 and row["review_calls"] == 1 and len(row["branches"]) == 1, row
