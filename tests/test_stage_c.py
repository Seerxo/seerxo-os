"""Stage C building blocks: repository lease/fencing, worktree isolation, fingerprint, lifecycle."""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import models, stage_c

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(minutes=30)


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return Session(engine)


def test_one_holder_per_repository_and_stale_token_is_fenced():
    db = _db()
    a = stage_c.acquire(db, "sez-company", "TASK-1", TTL, now=NOW)
    assert a and stage_c.acquire(db, "sez-company", "TASK-2", TTL, now=NOW) is None
    # Different repositories never share a lease.
    yt = stage_c.acquire(db, "youtube-auto", "TASK-2", TTL, now=NOW)
    assert yt and stage_c.holds(db, "youtube-auto", yt, now=NOW)
    later = NOW + TTL + timedelta(seconds=1)
    # An expired lease stops holding even before anyone else takes it.
    assert not stage_c.holds(db, "youtube-auto", yt, now=later)
    # Holder crashes; after expiry another task takes over with a higher token.
    b = stage_c.acquire(db, "sez-company", "TASK-2", TTL, now=later)
    assert b > a
    # Break caught: the crashed holder wakes up and pushes over the new holder's work.
    assert not stage_c.holds(db, "sez-company", a, now=later)
    assert stage_c.holds(db, "sez-company", b, now=later)
    with pytest.raises(stage_c.LeaseLost):
        stage_c.release(db, "sez-company", a)
    stage_c.release(db, "sez-company", b)
    c = stage_c.acquire(db, "sez-company", "TASK-3", TTL, now=later)
    assert c > b  # tokens stay monotonic across release


def test_worktree_is_isolated_and_task_key_cannot_escape(tmp_path):
    mirror = tmp_path / "mirror"
    git = lambda *a: subprocess.run(["git", "-C", str(mirror), *a], check=True, capture_output=True)
    mirror.mkdir()
    git("init", "-q", "-b", "main")
    (mirror / "f.txt").write_text("x")
    git("add", "f.txt")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    ws = tmp_path / "workspaces"
    ws.mkdir()
    path = stage_c.prepare_worktree(mirror, ws, "TASK-0042", "main")
    assert (path / "f.txt").read_text() == "x"
    branch = subprocess.run(["git", "-C", str(path), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "task/TASK-0042"
    with pytest.raises(FileExistsError):
        stage_c.prepare_worktree(mirror, ws, "TASK-0042", "main")
    for bad in ("../../etc", "TASK-1/../x", "task-1", ""):
        with pytest.raises(ValueError):
            stage_c.prepare_worktree(mirror, ws, bad, "main")


def test_fingerprint_binds_exact_bytes():
    assert stage_c.diff_fingerprint(b"a\n") == stage_c.diff_fingerprint(b"a\n")
    assert stage_c.diff_fingerprint(b"a\n") != stage_c.diff_fingerprint(b"a \n")


def test_lifecycle_is_stepwise_audited_and_merge_is_founder_only():
    db = _db()
    task = models.Task(key="TASK-0042", title="t", success_criteria="s")
    db.add(task)
    db.flush()
    for s in ("DETECTED", "PLANNED", "IMPLEMENTING", "TESTING", "IMPLEMENTING", "TESTING",
              "INDEPENDENT_REVIEW", "PR_OPEN", "CI", "READY_TO_MERGE"):
        stage_c.advance(db, task, s, "company-worker")
    with pytest.raises(PermissionError):
        stage_c.advance(db, task, "MERGED", "company-worker")
    stage_c.advance(db, task, "MERGED", "founder", {"pr": 60})
    with pytest.raises(ValueError):
        stage_c.advance(db, task, "ABANDONED", "company-worker")  # merged work is not abandoned
    with pytest.raises(ValueError):
        stage_c.advance(db, task, "MEASURED", "company-worker")  # no skipping VERIFYING
    assert task.context["improvement"]["stage"] == "MERGED"
    assert len(task.context["improvement"]["history"]) == 11
    db.flush()
    assert db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "improvement.advance")).all().__len__() == 11


def test_skipping_review_is_refused():
    db = _db()
    task = models.Task(key="TASK-0043", title="t", success_criteria="s")
    db.add(task)
    for s in ("DETECTED", "PLANNED", "IMPLEMENTING", "TESTING"):
        stage_c.advance(db, task, s, "company-worker")
    # Break caught: tested code goes straight to a PR without an independent review.
    with pytest.raises(ValueError):
        stage_c.advance(db, task, "PR_OPEN", "company-worker")


def test_returning_to_implementation_revokes_the_previous_review_approval():
    db = _db()
    task = models.Task(key="TASK-0044", title="t", success_criteria="s")
    db.add(task)
    db.flush()
    for s in ("DETECTED", "PLANNED", "IMPLEMENTING", "TESTING", "INDEPENDENT_REVIEW"):
        stage_c.advance(db, task, s, "company-worker")
    task.context = {**task.context, "improvement": {**task.context["improvement"],
                    "approval": {"fingerprint": "f" * 64}}}
    stage_c.advance(db, task, "IMPLEMENTING", "company-worker")
    assert "approval" not in task.context["improvement"]


def test_desk_lists_improvements_in_flight_with_latest_evidence():
    from app import desk
    db = _db()
    live = models.Task(key="TASK-0050", title="fix ci", success_criteria="s")
    done = models.Task(key="TASK-0051", title="old", success_criteria="s")
    plain = models.Task(key="TASK-0052", title="no lifecycle", success_criteria="s")
    db.add_all([live, done, plain])
    db.flush()
    stage_c.advance(db, live, "DETECTED", "repo-guardian", {"repository": "o/r", "risk": "MEDIUM"})
    stage_c.advance(db, live, "PLANNED", "company-worker", {"branch": "task/TASK-0050"})
    stage_c.advance(db, done, "DETECTED", "repo-guardian")
    stage_c.advance(db, done, "ABANDONED", "founder")
    rows = desk.improvements([live, done, plain])
    assert [r["task"] for r in rows] == ["TASK-0050"]
    assert rows[0] | {"updated_at": None} == {
        "task": "TASK-0050", "title": "fix ci", "stage": "PLANNED", "updated_at": None,
        "repository": "o/r", "branch": "task/TASK-0050", "pr_number": None, "pr_state": None,
        "ci_state": None, "failed_check": None, "repairs": None, "review_verdict": None,
        "risk": "MEDIUM", "expected_effect": None, "verified_at": None}
