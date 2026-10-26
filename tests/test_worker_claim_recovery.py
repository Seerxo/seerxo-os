"""Recovery of a claim whose worker died before its run began (worker._requeue_unstarted_claim)."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import audit, enums, models, worker
from tests.test_worker import _db, _task


@pytest.fixture
def clock(monkeypatch):
    now = [worker._now()]
    monkeypatch.setattr(worker, "_now", lambda: now[0])
    return now


def _age(db, task, clock):
    clock[0] += worker.STALE_AFTER + timedelta(minutes=5)
    task.updated_at = clock[0] - worker.STALE_AFTER - timedelta(minutes=1)
    db.commit()


def _founder(db, task, status):
    task.status = status
    audit.record(db, actor="founder:access:founder@example.invalid", action="task.status", entity_type="task",
                 entity_key=task.key, task_id=task.id)
    db.commit()


def _older_run_then_newer_unstarted_claim(db, clock):
    """Claim 1 runs; the founder sets READY while that run is still STARTED; claim 2's worker dies before its run."""
    task = _task(db)
    assert worker.claim(db, task)
    db.add(models.AgentRun(agent_id=task.assigned_agent_id, task_id=task.id, provider="claude",
                           status=enums.AgentRunStatus.STARTED, started_at=clock[0]))
    db.commit()
    _founder(db, task, enums.TaskStatus.READY)
    assert worker.claim(db, task)
    return task


def test_superseded_run_bookkeeping_does_not_strand_a_newer_unstarted_claim(clock):
    # Codex P1 on #370: recover_stale supersedes the old run and writes task.autonomous-run.superseded after the
    # newer claim; _requeue_unstarted_claim required the newest audit row to be that claim, so the task stayed
    # IN_PROGRESS forever.
    db = _db()
    task = _older_run_then_newer_unstarted_claim(db, clock)
    _age(db, task, clock)
    worker.recover_stale(db)                       # supersedes the old run
    _age(db, task, clock)
    worker.recover_stale(db)                       # the newer claim is stale and never started a run
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.retry_count == 0
    actions = list(db.scalars(select(models.AuditLog.action).where(models.AuditLog.task_id == task.id)
                              .order_by(models.AuditLog.id)))
    assert actions[-1] == "task.claim.recovered" and "task.autonomous-run.superseded" in actions


@pytest.mark.parametrize("founder_status", [enums.TaskStatus.IN_PROGRESS, enums.TaskStatus.BLOCKED])
def test_a_founder_action_after_the_unstarted_claim_is_never_lost(clock, founder_status):
    # The founder acted after the claim (a manual IN_PROGRESS touch, or a block): recovery must not requeue it,
    # even with worker bookkeeping rows written after the founder's row.
    db = _db()
    task = _older_run_then_newer_unstarted_claim(db, clock)
    _founder(db, task, founder_status)
    _age(db, task, clock)
    worker.recover_stale(db)
    _age(db, task, clock)
    worker.recover_stale(db)
    db.refresh(task)
    assert task.status == founder_status
    assert "task.claim.recovered" not in set(db.scalars(select(models.AuditLog.action).where(
        models.AuditLog.task_id == task.id)))


def test_repository_guardian_evidence_during_a_run_is_not_an_owner_change(tmp_path):
    """repo-guardian refreshes CI evidence on an open finding (audit actor "repo-guardian"). That is automation,
    not an owner change: it must not supersede the attempt and strand the task IN_PROGRESS; the failure requeues."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.providers import Health, ProviderResult
    from tests.test_worker import SPECS, FakeProvider

    engine = create_engine(f"sqlite:///{tmp_path / 'guardian.db'}")
    models.Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(models.Agent(key="research", role="research", enabled=True))
        db.commit()
        task = _task(db)
        task_id = task.id

        class GuardianDuringRun(FakeProvider):
            def _run(self, *args, **kwargs):
                with Session(engine) as other:
                    other.add(models.AuditLog(actor="repo-guardian", action="task.guardian_evidence",
                                              entity_type="task", entity_key="TASK-0100", task_id=task_id))
                    other.commit()
                return super()._run(*args, **kwargs)

        out = worker.run_once(db, {"claude": GuardianDuringRun("claude", [
            ProviderResult("claude", Health.AVAILABLE, error="timed out")])}, SPECS)
    with Session(engine) as check:
        task = check.get(models.Task, task_id)
        assert out["verdict"] != "SUPERSEDED"
        assert task.status == enums.TaskStatus.READY and task.retry_count == 1
