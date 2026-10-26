"""Worker-level Stage C guard against real PostgreSQL: a published task is never re-coded
after its PR was merged outside the gate, and a failed GitHub lookup runs no coder.

Same disposable-database guard as the other PostgreSQL tests: CI fails rather than skips
when its URL is missing, and only the disposable test database is allowed.
"""
import os
import subprocess

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import audit, enums, models, stage_c_ci, stage_c_runner, stage_c_worker, worker
from tests.test_stage_c_worker import _same_repository_publishing, CODER_CALLS, Reviewer, SPECS, _coder_spy, _launcher_must_not_run, \
    _merged_outside_gate, _published_then_repair, pipeline, write_task

REQUIRED_CONTEXTS = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")
GREEN_STATUSES = {context: "success" for context in REQUIRED_CONTEXTS}


def ingest_ci(*args, **kwargs):
    """ingest with the policy's required commit statuses (the production contract)."""
    return stage_c_ci.ingest(*args, required_statuses=REQUIRED_CONTEXTS, **kwargs)


@pytest.fixture
def db():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    with Session(engine) as session:
        for key in ("ceo", "developer", "supervisor", "research"):  # the same agents _db() seeds
            session.add(models.Agent(key=key, role=key, enabled=True))
        session.commit()
        yield session
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def test_postgres_merged_after_failed_ci_never_reaches_the_coder(db, monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}
    commit = _published_then_repair(db, task, ps, monkeypatch)
    history = list(task.context["improvement"]["history"])
    _coder_spy(monkeypatch)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *a, **k: _merged_outside_gate(commit))
    worker.run_once(db, ps, SPECS)
    db.commit()
    assert CODER_CALLS == []
    fresh = db.query(models.Task).filter_by(key=task.key).one()
    assert fresh.status == enums.TaskStatus.BLOCKED
    assert fresh.context["improvement"]["stage"] == "IMPLEMENTING"
    assert fresh.context["improvement"]["history"] == history


def test_postgres_failed_lookup_fails_closed_without_a_coder(db, monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}
    _published_then_repair(db, task, ps, monkeypatch)
    _coder_spy(monkeypatch)

    def unreachable(*a, **k):
        raise RuntimeError("GitHub read failed")
    monkeypatch.setattr(stage_c_ci, "gh_facts", unreachable)
    out = worker.run_once(db, ps, SPECS)
    db.commit()
    assert CODER_CALLS == [] and out.get("blocked") == "pr_state_unverified"
    assert stage_c_worker.published_pr(task) is not None


def test_postgres_founder_change_during_the_lookup_is_never_overwritten(db, monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}
    commit = _published_then_repair(db, task, ps, monkeypatch)

    def founder_cancels_then_merged(*a, **k):
        db.execute(models.Task.__table__.update().where(models.Task.id == task.id)
                   .values(status=enums.TaskStatus.CANCELLED))
        db.commit()
        return _merged_outside_gate(commit)
    monkeypatch.setattr(stage_c_ci, "gh_facts", founder_cancels_then_merged)
    assert stage_c_worker.published_guard(db, task) == "task_changed"
    db.expire_all()
    assert db.get(models.Task, task.id).status == enums.TaskStatus.CANCELLED


def test_postgres_merge_after_the_claim_never_reaches_the_coder(db, monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}
    commit = _published_then_repair(db, task, ps, monkeypatch)
    open_facts = {"state": "open", "merged": False, "head_sha": commit, "checks_complete": True, "statuses": dict(GREEN_STATUSES), "statuses_complete": True, "status_problems": [],
                  "checks": [{"name": "ci", "status": "completed", "conclusion": "failure"}]}
    reads = []

    def lookups(*a, **k):
        reads.append(1)
        return dict(open_facts) if len(reads) <= 2 else _merged_outside_gate(commit)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lookups)
    launched = _launcher_must_not_run(monkeypatch)
    worker.run_once(db, ps, SPECS)
    db.commit()
    assert launched == []
    fresh = db.query(models.Task).filter_by(key=task.key).one()
    assert fresh.status == enums.TaskStatus.BLOCKED and fresh.context["improvement"]["stage"] == "IMPLEMENTING"


def _takeover_in_its_own_session(bind, task_id, task_key, agent_key):
    """A founder's assignment committed from another connection, as the API would do it."""
    with Session(bind) as other:
        agent = other.query(models.Agent).filter_by(key=agent_key).one()
        other.execute(models.Task.__table__.update().where(models.Task.id == task_id)
                      .values(assigned_agent_id=agent.id))
        other.add(models.AuditLog(actor="founder:access:founder@example.invalid", action="task.assign",
                                  entity_type="task", entity_key=task_key, task_id=task_id,
                                  after={"assigned_agent_id": agent.id}))
        other.commit()


def test_postgres_owner_change_during_coder_writes_no_later_pipeline_state(db, monkeypatch, tmp_path):
    hooks = {}
    pipeline(monkeypatch, tmp_path, hooks)
    task = write_task(db)
    at_transfer = []

    def transfer():
        _takeover_in_its_own_session(db.get_bind(), task.id, task.key, "ceo")
        at_transfer.append(db.scalar(select(func.max(models.AuditLog.id))))
    hooks["after_coder_start"] = transfer
    worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    db.expire_all()
    assert db.get(models.Task, task.id).status == enums.TaskStatus.BLOCKED
    assert not list(db.scalars(select(models.AuditLog.id).where(
        models.AuditLog.task_id == task.id, models.AuditLog.id > at_transfer[0],
        models.AuditLog.action == "improvement.advance")))
    assert list(db.scalars(select(models.Event.event_type).where(
        models.Event.task_id == task.id, models.Event.event_type == "TASK_BLOCKED"))) == ["TASK_BLOCKED"]


def test_postgres_cancel_during_review_writes_no_later_approval(db, monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    task = write_task(db)

    def review(packet):
        with Session(db.get_bind()) as founder:
            changed = founder.scalar(select(models.Task).where(models.Task.id == task.id).with_for_update())
            changed.status = enums.TaskStatus.CANCELLED
            audit.record(founder, actor="founder:access:founder@example.invalid", action="task.status",
                         entity_type="task", entity_key=task.key, task_id=task.id)
            founder.commit()
        return {"verdict": "APPROVE", "fingerprint": packet["fingerprint"], "findings": []}
    monkeypatch.setattr(stage_c_runner, "provider_reviewer", lambda provider: review)
    worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    db.expire_all()
    fresh = db.get(models.Task, task.id)
    assert fresh.status == enums.TaskStatus.CANCELLED and "approval" not in fresh.context["improvement"]
    assert not list(db.scalars(select(models.AuditLog.id).where(
        models.AuditLog.task_id == task.id, models.AuditLog.action == "improvement.review_approved")))


def test_postgres_takeover_committed_during_the_lookup_is_detected(db, monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}
    commit = _published_then_repair(db, task, ps, monkeypatch)
    task.status = enums.TaskStatus.IN_PROGRESS
    db.commit()
    open_facts = {"state": "open", "merged": False, "head_sha": commit, "checks_complete": True, "statuses": dict(GREEN_STATUSES), "statuses_complete": True, "status_problems": [],
                  "checks": [{"name": "ci", "status": "completed", "conclusion": "failure"}]}

    def lookup(*a, **k):
        _takeover_in_its_own_session(db.get_bind(), task.id, task.key, "ceo")
        return dict(open_facts)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lookup)
    assert stage_c_worker.published_guard(db, task) == "task_changed"
    db.expire_all()
    fresh = db.query(models.Task).filter_by(key=task.key).one()
    assert fresh.status == enums.TaskStatus.IN_PROGRESS
    assert fresh.assigned_agent_id == db.query(models.Agent).filter_by(key="ceo").one().id


def test_postgres_takeover_waits_for_the_guard_lock_then_applies_after_it(db, monkeypatch, tmp_path):
    import threading
    from sqlalchemy import select
    pipeline(monkeypatch, tmp_path)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}
    _published_then_repair(db, task, ps, monkeypatch)
    bind = db.get_bind()
    holding = threading.Event()
    finished = {}
    with Session(bind) as guard:
        # The guard's critical section: the row is locked, and it reads the owner state under the lock.
        _, seen = stage_c_worker.owner_state(guard, task.id, lock=True)
        def founder():
            holding.set()
            _takeover_in_its_own_session(bind, task.id, task.key, "ceo")
            finished["done"] = True
        t = threading.Thread(target=founder)
        t.start()
        holding.wait(5)
        t.join(timeout=0.5)
        assert t.is_alive(), "the takeover must wait for the guard's row lock"
        guard.commit()  # the guard releases the lock without changing anything
        t.join(timeout=10)
        assert finished.get("done") is True
    # The takeover was applied only after the guard: the guard's read predates it, the row now reflects it.
    later = stage_c_worker.owner_state(db, task.id)[1]
    assert later[2] > seen[2], "the takeover must advance the owner version after the guard read it"
    assert later[1] == db.query(models.Agent).filter_by(key="ceo").one().id


def test_postgres_takeover_waits_for_coder_start_then_blocks_publication(db, monkeypatch, tmp_path):
    import threading
    hooks, state = {}, {}
    remote = pipeline(monkeypatch, tmp_path, hooks)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}

    def founder_waits_for_start_ack():
        started = threading.Event()
        def founder():
            started.set()
            _takeover_in_its_own_session(db.get_bind(), task.id, task.key, "ceo")
            state["committed"] = True
        thread = threading.Thread(target=founder)
        thread.start()
        assert started.wait(5)
        thread.join(timeout=0.5)
        state["blocked_during_start"] = thread.is_alive()
        state["thread"] = thread

    def wait_for_takeover_commit():
        state["thread"].join(timeout=10)

    hooks["during_coder_start"] = founder_waits_for_start_ack
    hooks["after_coder_start"] = wait_for_takeover_commit
    out = worker.run_once(db, ps, SPECS)
    assert state["blocked_during_start"] is True and state.get("committed") is True
    assert out["verdict"] == "PUBLISHED_PR_REFUSED"
    assert out["model_calls"] == 1  # coder started before the takeover; review and publication are skipped
    assert subprocess.run(["git", "--git-dir", str(remote), "show-ref", "--verify", "--quiet",
                           f"refs/heads/task/{task.key}"]).returncode == 1
    db.expire_all()
    fresh = db.query(models.Task).filter_by(key=task.key).one()
    assert fresh.assigned_agent_id == db.query(models.Agent).filter_by(key="ceo").one().id
    assert stage_c_worker.published_pr(fresh) is None


def test_postgres_takeover_waits_until_the_pr_is_published_and_recorded(db, monkeypatch, tmp_path):
    import threading
    hooks, state = {}, {}
    pipeline(monkeypatch, tmp_path, hooks)
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}

    def takeover_during_open_pr(db, task):
        started = threading.Event()
        def founder():
            started.set()
            _takeover_in_its_own_session(db.get_bind(), task.id, task.key, "ceo")
            state["committed"] = True
        thread = threading.Thread(target=founder)
        thread.start()
        assert started.wait(5)
        thread.join(timeout=0.5)
        state["blocked_during_publication"] = thread.is_alive()
        state["thread"] = thread

    hooks["during_open_pr"] = takeover_during_open_pr
    out = worker.run_once(db, ps, SPECS)
    state["thread"].join(timeout=10)
    assert state["blocked_during_publication"] is True
    assert state.get("committed") is True and out["verdict"] == "PR_OPEN"
    db.expire_all()
    fresh = db.query(models.Task).filter_by(key=task.key).one()
    assert fresh.assigned_agent_id == db.query(models.Agent).filter_by(key="ceo").one().id
    assert stage_c_worker.published_pr(fresh) is not None
