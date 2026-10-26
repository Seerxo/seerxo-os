"""A completed read-only provider result survives a worker crash before finalization."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import control, enums, models, worker
from tests.test_worker import SPECS, FakeProvider, _good, _task


def test_restart_finishes_the_saved_result_without_another_model_call(saved_result):
    engine, task_id, _, _ = saved_result(actions=["Persist this completed answer."])
    url = engine.url
    engine.dispose()
    # A new engine/session represents a replacement process with no in-memory result.
    restarted = create_engine(url)
    with Session(restarted) as db:
        replacement = FakeProvider("claude", [])
        outcome = worker.run_once(db, {"claude": replacement}, SPECS)
        task = db.get(models.Task, task_id)
        runs = db.scalars(select(models.AgentRun)).all()

        assert task.status == enums.TaskStatus.REVIEW
        assert task.artifacts["actions"] == ["Persist this completed answer."]
        assert task.retry_count == 0
        assert len(runs) == 1 and runs[0].status == enums.AgentRunStatus.SUCCEEDED
        assert replacement.calls == 0 and outcome["model_calls"] == 0
        assert worker.run_once(db, {"claude": replacement}, SPECS)["model_calls"] == 0


@pytest.mark.parametrize("bookkeeping", ["cooldown", "cli_version"])
def test_post_provider_bookkeeping_cannot_lose_a_completed_answer(monkeypatch, bookkeeping):
    from tests.test_worker import _db

    db = _db()
    task = _task(db)
    provider = FakeProvider("claude", [_good(task.key)])

    def crash(*args, **kwargs):
        raise SystemExit("terminated during post-provider bookkeeping")

    with monkeypatch.context() as fault:
        if bookkeeping == "cooldown":
            fault.setattr(worker, "_cool", crash)
        else:
            fault.setattr(provider, "version", crash)
        with pytest.raises(SystemExit):
            worker.run_once(db, {"claude": provider}, SPECS)
    db.rollback()
    run = db.scalar(select(models.AgentRun))
    assert provider.calls == 1 and run.tool_calls["result_checkpoint"]["output"] == _good(task.key)
    replacement = FakeProvider("claude", [])
    resumed_at = worker._now() + worker.STALE_AFTER + timedelta(seconds=1)
    monkeypatch.setattr(worker, "_now", lambda: resumed_at)
    outcome = worker.run_once(db, {"claude": replacement}, SPECS)
    assert outcome["model_calls"] == 0 and replacement.calls == 0
    assert task.status == enums.TaskStatus.REVIEW


@pytest.fixture
def saved_result(tmp_path, monkeypatch):
    clock = [worker._now()]
    monkeypatch.setattr(worker, "_now", lambda: clock[0])

    def prepare(**output):
        engine = create_engine(f"sqlite:///{tmp_path / 'saved.db'}")
        models.Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(models.Agent(key="research", role="research", enabled=True))
            db.commit()
            task = _task(db)
            task_id = task.id
            with monkeypatch.context() as fault:
                def crash(*args, **kwargs):
                    raise SystemExit("process terminated during result persistence")

                fault.setattr(worker.task_materializer, "materialize", crash)
                with pytest.raises(SystemExit):
                    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, **output)])}, SPECS)
            db.rollback()
            run_id = db.scalar(select(models.AgentRun.id))
        clock[0] += worker.STALE_AFTER + timedelta(seconds=5)
        return engine, task_id, run_id, clock

    return prepare


@pytest.mark.parametrize("status", [enums.TaskStatus.BLOCKED, enums.TaskStatus.CANCELLED, enums.TaskStatus.DONE])
def test_recovery_never_overwrites_a_founder_state_change(saved_result, status):
    engine, task_id, run_id, _ = saved_result()
    with Session(engine) as db:
        task = db.get(models.Task, task_id)
        task.status, task.error = status, "Founder decision"
        db.commit()
        worker.recover_stale(db)
        assert task.status == status and task.error == "Founder decision"
        assert db.get(models.AgentRun, run_id).status == enums.AgentRunStatus.FAILED


def test_changed_task_contract_is_blocked_without_reusing_the_old_answer(saved_result):
    engine, task_id, run_id, _ = saved_result()
    with Session(engine) as db:
        task = db.get(models.Task, task_id)
        task.objective = "A different task the saved answer does not satisfy"
        db.commit()
        worker.recover_stale(db)
        assert task.status == enums.TaskStatus.BLOCKED and task.result is None
        assert "no longer matches" in task.error
        assert "result_checkpoint" in db.get(models.AgentRun, run_id).tool_calls


@pytest.mark.parametrize("expire_on_commit", [True, False])
@pytest.mark.parametrize("change", ["objective", "agent_disabled"])
def test_result_uses_the_original_contract_and_current_permission(tmp_path, expire_on_commit, change):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-change.db'}")
    models.Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=expire_on_commit) as db:
        db.add(models.Agent(key="research", role="research", enabled=True))
        db.commit()
        task = _task(db)
        task_id, agent_id = task.id, task.assigned_agent_id

        class ChangedDuringRun(FakeProvider):
            def _run(self, *args, **kwargs):
                with Session(engine) as founder:
                    if change == "objective":
                        founder.get(models.Task, task_id).objective = "A different founder objective"
                    else:
                        founder.get(models.Agent, agent_id).enabled = False
                    founder.commit()
                return super()._run(*args, **kwargs)

        provider = ChangedDuringRun("claude", [_good(task.key)])
        outcome = worker.run_once(db, {"claude": provider}, SPECS)

        assert outcome["verdict"] == "RESULT_BLOCKED"
        assert task.status == enums.TaskStatus.BLOCKED and task.result is None
        run = db.scalar(select(models.AgentRun))
        assert run.status == enums.AgentRunStatus.FAILED and run.supervisor_verdict is None
        assert "result_checkpoint" in run.tool_calls and provider.calls == 1


def test_unknown_agent_registry_cannot_resume_saved_work(saved_result):
    engine, task_id, _, _ = saved_result()
    with Session(engine) as db:
        worker.recover_stale(db, specs={})
        task = db.get(models.Task, task_id)
        assert task.status == enums.TaskStatus.BLOCKED and "agent_not_in_registry" in task.error


def test_mixed_stale_runs_keep_earlier_recovery_when_a_saved_result_refreshes_state(saved_result):
    engine, _, _, clock = saved_result()
    with Session(engine) as db:
        cancelled = _task(db, key="TASK-0099", status=enums.TaskStatus.CANCELLED)
        legacy = models.AgentRun(id=0, task_id=cancelled.id, agent_id=cancelled.assigned_agent_id,
                                 provider="claude", status=enums.AgentRunStatus.STARTED,
                                 started_at=clock[0] - worker.STALE_AFTER - timedelta(seconds=1))
        db.add(legacy)
        db.commit()
        assert worker.recover_stale(db) == ["TASK-0099", "TASK-0100"]
        db.expire_all()
        assert legacy.status == enums.AgentRunStatus.FAILED and legacy.ended_at is not None
        assert cancelled.status == enums.TaskStatus.CANCELLED


@pytest.mark.parametrize("lease", ["far_future", "missing_expiry"])
def test_corrupt_lease_cannot_hold_a_saved_result_forever(saved_result, lease):
    engine, task_id, run_id, clock = saved_result()
    with Session(engine) as db:
        run = db.get(models.AgentRun, run_id)
        saved = {**run.tool_calls["result_checkpoint"], "claim": "a" * 32,
                 "claim_until": (clock[0] + timedelta(days=365)).isoformat()}
        if lease == "missing_expiry":
            saved.pop("claim_until")
        run.tool_calls = {**run.tool_calls, "result_checkpoint": saved}
        db.commit()
        worker.recover_stale(db)
        assert db.get(models.Task, task_id).status == enums.TaskStatus.BLOCKED


def test_pause_holds_finalization_then_resume_needs_no_available_provider(saved_result):
    from app.providers import Health

    engine, task_id, _, _ = saved_result()
    with Session(engine) as db:
        control.set_agents_paused(db, True, actor="founder", reason="test pause")
        assert worker.recover_stale(db) == []
        assert db.get(models.Task, task_id).status == enums.TaskStatus.IN_PROGRESS
        control.set_agents_paused(db, False, actor="founder", reason="test resume")
        provider = FakeProvider("claude", [], health=Health.QUOTA_EXHAUSTED)
        outcome = worker.run_once(db, {"claude": provider}, SPECS)
        assert db.get(models.Task, task_id).status == enums.TaskStatus.REVIEW
        assert outcome["model_calls"] == 0 and provider.calls == 0


@pytest.mark.parametrize("bad", [None, [], {"version": 99}, "corrupt", {"version": True}])
def test_malformed_saved_results_fail_closed(saved_result, bad):
    engine, task_id, run_id, _ = saved_result()
    with Session(engine) as db:
        run = db.get(models.AgentRun, run_id)
        run.tool_calls = {**run.tool_calls, "result_checkpoint": bad}
        db.commit()
        worker.recover_stale(db)
        task = db.get(models.Task, task_id)
        assert task.status == enums.TaskStatus.BLOCKED and task.retry_count == 0
        assert task.result is None


def test_repeated_finalization_crashes_are_bounded_and_keep_the_answer(saved_result, monkeypatch):
    engine, task_id, run_id, clock = saved_result()

    def crash(*args, **kwargs):
        raise SystemExit("same persistence failure")

    monkeypatch.setattr(worker.task_materializer, "materialize", crash)
    for _ in range(worker.MAX_RESULT_FINALIZATIONS - 1):
        with Session(engine) as db:
            with pytest.raises(SystemExit):
                worker.recover_stale(db)
            db.rollback()
        clock[0] += worker.RESULT_CLAIM_TTL + timedelta(seconds=1)
    with Session(engine) as db:
        worker.recover_stale(db)
        task, run = db.get(models.Task, task_id), db.get(models.AgentRun, run_id)
        assert task.status == enums.TaskStatus.BLOCKED and task.retry_count == 0
        assert "attempts exhausted" in task.error
        assert run.tool_calls["result_checkpoint"]["finalizations"] == worker.MAX_RESULT_FINALIZATIONS
        assert worker.recover_stale(db) == []


def test_result_memory_and_one_child_commit_together_after_restart(saved_result, monkeypatch):
    monkeypatch.setenv("PROPOSAL_AUTO_READY", "true")
    engine, task_id, _, _ = saved_result(
        learning_candidates=["Preserve completed results before creating follow-up work."],
        new_task_candidates=[{"title": "Analyze the measured audience", "reason": "Use recorded observations",
                              "kind": "analysis"}])
    with Session(engine) as db:
        assert db.scalars(select(models.AgentNote)).all() == []
        worker.recover_stale(db)
        task = db.get(models.Task, task_id)
        child = db.scalars(select(models.Task).where(models.Task.id != task_id)).one()
        note = db.scalars(select(models.AgentNote)).one()
        assert task.status == enums.TaskStatus.REVIEW and child.status == enums.TaskStatus.READY
        assert note.reference_count == 1
        assert worker.recover_stale(db) == []
        provider = FakeProvider("claude", [_good(child.key)])
        assert worker.run_once(db, {"claude": provider}, SPECS)["task"] == child.key
        assert worker.run_once(db, {"claude": provider}, SPECS)["model_calls"] == 0
        assert len(db.scalars(select(models.Task)).all()) == 2


def test_notification_crash_cannot_erase_the_completed_transition_audit(monkeypatch):
    from tests.test_worker import _db

    db = _db()
    task = _task(db)

    def crash(db, event):
        if event.event_type == "TASK_COMPLETED":
            raise SystemExit("notification delivery interrupted after commit")

    monkeypatch.setattr(worker.notifier, "dispatch", crash)
    with pytest.raises(SystemExit):
        worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key)])}, SPECS)
    db.rollback()
    assert task.status == enums.TaskStatus.REVIEW
    assert db.scalar(select(models.AuditLog).where(models.AuditLog.action == "task.autonomous-run")) is not None
