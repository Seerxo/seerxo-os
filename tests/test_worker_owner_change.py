"""Founder decision D1/D2: a genuine ownership change (assigned_agent_id really changed) while a worker attempt
holds an IN_PROGRESS task is BLOCKED with a TASK_BLOCKED event, under the row lock and the ownership check. A
founder status decision, a cancel or a newer claim is never overwritten, and nothing moves BLOCKED back to READY.
"""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import enums, models, worker
from app.providers import Health, ProviderResult
from tests.test_worker import SPECS, FakeProvider, _good, _task

T = enums.TaskStatus
FOUNDER = "founder:access:founder@example.invalid"


def _setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'owner.db'}")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Agent(key="research", role="research", enabled=True))
        db.add(models.Agent(key="other", role="research", enabled=True))
        db.commit()
        task = _task(db)
        other = db.scalar(select(models.Agent.id).where(models.Agent.key == "other"))
        return engine, task.id, other


def _in_own_session(engine, task_id, *, agent=None, status=None):
    """The founder (or an operator) acts on its own connection while the provider runs."""
    with Session(engine) as s:
        t = s.get(models.Task, task_id)
        if agent is not None:
            t.assigned_agent_id = agent
            s.add(models.AuditLog(actor=FOUNDER, action="task.assign", entity_type="task", entity_key=t.key,
                                  task_id=task_id))
        if status is not None:
            t.status = status
            s.add(models.AuditLog(actor=FOUNDER, action="task.status", entity_type="task", entity_key=t.key,
                                  task_id=task_id))
        s.commit()


class During(FakeProvider):
    def __init__(self, outputs, during):
        super().__init__("claude", outputs)
        self.during = during

    def _run(self, *args, **kwargs):
        self.during()
        return super()._run(*args, **kwargs)


ANSWERS = {"good": lambda key: _good(key),
           "error": lambda key: ProviderResult("claude", Health.AVAILABLE, error="timed out"),
           "no_capacity": lambda key: ProviderResult("claude", Health.QUOTA_EXHAUSTED, error="usage limit")}


def _events(db, task_id):
    return list(db.scalars(select(models.Event.event_type).where(models.Event.task_id == task_id)))


@pytest.mark.parametrize("answer", sorted(ANSWERS))
def test_a_genuine_reassignment_mid_run_blocks_the_task_with_an_event(tmp_path, monkeypatch, answer):
    clock = [worker._now()]
    monkeypatch.setattr(worker, "_now", lambda: clock[0])
    engine, task_id, other = _setup(tmp_path)
    provider = During([ANSWERS[answer]("TASK-0100")], lambda: _in_own_session(engine, task_id, agent=other))
    with Session(engine, expire_on_commit=False) as db:
        out = worker.run_once(db, {"claude": provider}, SPECS)
        assert out["verdict"] == "SUPERSEDED"
        for _ in range(2):                              # later ticks never requeue or strand it
            clock[0] += worker.STALE_AFTER * 5
            worker.recover_stale(db)
    with Session(engine) as db:
        t = db.get(models.Task, task_id)
        assert (t.status, t.error, t.result, t.retry_count) == (T.BLOCKED, worker.OWNER_CHANGED_BLOCK, None, 0)
        assert t.assigned_agent_id == other
        assert "TASK_BLOCKED" in _events(db, task_id)
        assert db.scalar(select(models.AgentRun)).status == enums.AgentRunStatus.FAILED
        assert not any((a.after or {}).get("status") == "READY" for a in db.scalars(
            select(models.AuditLog).where(models.AuditLog.task_id == task_id)))


@pytest.mark.parametrize("status", [T.IN_PROGRESS, T.CANCELLED, T.DONE])
def test_a_founder_status_decision_with_the_reassignment_is_never_overwritten(tmp_path, status):
    # Reassigned AND a founder status decision (manual IN_PROGRESS, cancel, close): the founder's state stands.
    engine, task_id, other = _setup(tmp_path)
    provider = During([_good("TASK-0100")], lambda: _in_own_session(engine, task_id, agent=other, status=status))
    with Session(engine, expire_on_commit=False) as db:
        assert worker.run_once(db, {"claude": provider}, SPECS)["verdict"] == "SUPERSEDED"
    with Session(engine) as db:
        t = db.get(models.Task, task_id)
        assert t.status == status and t.result is None and "TASK_BLOCKED" not in _events(db, task_id)


def test_a_status_only_founder_touch_preserves_the_founder_state_without_blocking(tmp_path):
    engine, task_id, _ = _setup(tmp_path)
    provider = During([_good("TASK-0100")], lambda: _in_own_session(engine, task_id, status=T.IN_PROGRESS))
    with Session(engine, expire_on_commit=False) as db:
        assert worker.run_once(db, {"claude": provider}, SPECS)["verdict"] == "SUPERSEDED"
    with Session(engine) as db:
        t = db.get(models.Task, task_id)
        assert t.status == T.IN_PROGRESS and t.result is None and "TASK_BLOCKED" not in _events(db, task_id)


def test_a_reassignment_never_blocks_a_newer_claim(tmp_path):
    # Reassigned, requeued and claimed again by a newer attempt: the old run must not block the new claim.
    engine, task_id, other = _setup(tmp_path)

    def reassign_requeue_reclaim():
        _in_own_session(engine, task_id, agent=other, status=T.READY)
        with Session(engine) as s:
            assert worker.claim(s, s.get(models.Task, task_id))
    provider = During([_good("TASK-0100")], reassign_requeue_reclaim)
    with Session(engine, expire_on_commit=False) as db:
        assert worker.run_once(db, {"claude": provider}, SPECS)["verdict"] == "SUPERSEDED"
    with Session(engine) as db:
        t = db.get(models.Task, task_id)
        assert t.status == T.IN_PROGRESS and "TASK_BLOCKED" not in _events(db, task_id)


def test_stale_recovery_of_a_reassigned_run_blocks_instead_of_stranding(tmp_path, monkeypatch):
    # The worker died mid-run after a genuine reassignment: recovery blocks the task (never strands, never READY).
    clock = [worker._now()]
    monkeypatch.setattr(worker, "_now", lambda: clock[0])
    engine, task_id, other = _setup(tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        task = db.get(models.Task, task_id)
        assert worker.claim(db, task)
        db.add(models.AgentRun(agent_id=task.assigned_agent_id, task_id=task_id, provider="claude",
                               status=enums.AgentRunStatus.STARTED, started_at=clock[0]))
        db.commit()
        _in_own_session(engine, task_id, agent=other)
        clock[0] += worker.STALE_AFTER * 5
        worker.recover_stale(db)
        clock[0] += worker.STALE_AFTER * 5
        worker.recover_stale(db)
    with Session(engine) as db:
        t = db.get(models.Task, task_id)
        assert (t.status, t.error) == (T.BLOCKED, worker.OWNER_CHANGED_BLOCK) and "TASK_BLOCKED" in _events(db, task_id)
