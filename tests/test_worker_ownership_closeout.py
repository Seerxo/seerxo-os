"""Closeout regressions: a lost task owner never writes pipeline or review state."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, enums, models, providers, stage_c_runner, worker
from tests.test_worker import SPECS, _db, _good, _task
from tests.test_worker_owner_change import During, _setup
from tests.test_stage_c_worker import (
    Reviewer, _founder_takes_over, _touch_after_coder, pipeline, write_task,
)


def test_cancel_during_fetch_preserves_existing_repair_artifacts(monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    db = _db()
    task = write_task(db)
    root = tmp_path / "jobs" / task.key
    (root / "done").mkdir(parents=True)
    marker = root / "done/result.json"
    marker.write_text('{"evidence":"keep"}')
    original = stage_c_runner._git

    def git_then_cancel(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if args[0] == "fetch":
            task.status = enums.TaskStatus.CANCELLED
            audit.record(db, actor="founder:access:founder@example.invalid", action="task.status",
                         entity_type="task", entity_key=task.key, task_id=task.id)
            db.commit()
        return result
    monkeypatch.setattr(stage_c_runner, "_git", git_then_cancel)
    worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    assert task.status == enums.TaskStatus.CANCELLED
    assert marker.exists() and marker.read_text() == '{"evidence":"keep"}'


def test_owner_change_during_coder_allows_no_later_pipeline_side_effects(monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    db = _db()
    task = write_task(db)
    at_transfer = []
    def transfer():
        _founder_takes_over(db, task)
        at_transfer.append(db.scalar(select(models.AuditLog.id).order_by(models.AuditLog.id.desc()).limit(1)))
    _touch_after_coder(monkeypatch, transfer)
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    db.expire_all()
    task = db.get(models.Task, task.id)
    later_advances = list(db.scalars(select(models.AuditLog.after).where(
        models.AuditLog.task_id == task.id, models.AuditLog.id > at_transfer[0],
        models.AuditLog.action == "improvement.advance")))
    assert task.status == enums.TaskStatus.BLOCKED
    assert later_advances == [], {"out": out, "later_advances": later_advances}


def test_cancel_during_independent_review_allows_no_later_review_side_effects(monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path)
    db = _db()
    task = write_task(db)
    original = stage_c_runner.provider_reviewer
    at_cancel = []
    def wrapping(provider):
        call = original(provider)
        def reviewer(packet):
            task.status = enums.TaskStatus.CANCELLED
            audit.record(db, actor="founder:access:founder@example.invalid", action="task.status",
                         entity_type="task", entity_key=task.key, task_id=task.id)
            db.commit()
            at_cancel.append(db.scalar(select(models.AuditLog.id).order_by(models.AuditLog.id.desc()).limit(1)))
            return call(packet)
        return reviewer
    monkeypatch.setattr(stage_c_runner, "provider_reviewer", wrapping)
    worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    db.expire_all()
    task = db.get(models.Task, task.id)
    approvals = list(db.scalars(select(models.AuditLog.action).where(
        models.AuditLog.task_id == task.id, models.AuditLog.id > at_cancel[0],
        models.AuditLog.action == "improvement.review_approved")))
    assert task.status == enums.TaskStatus.CANCELLED
    assert approvals == [] and "approval" not in task.context["improvement"], approvals


@pytest.mark.parametrize("answer", ["good", "error", "capacity"])
def test_stage_b_real_assignment_change_without_audit_is_still_superseded(tmp_path, answer):
    engine, task_id, other = _setup(tmp_path)
    def transfer():
        with Session(engine) as db:
            db.get(models.Task, task_id).assigned_agent_id = other
            db.commit()
    result = (_good("TASK-0100") if answer == "good" else providers.ProviderResult("claude", providers.Health.AVAILABLE, error="timed out")
              if answer == "error" else providers.ProviderResult("claude", providers.Health.QUOTA_EXHAUSTED,
                                                                  error="usage limit"))
    with Session(engine) as db:
        out = worker.run_once(db, {"claude": During([result], transfer)}, SPECS)
    with Session(engine) as db:
        task = db.get(models.Task, task_id)
        assert out["verdict"] == "SUPERSEDED" and task.status == enums.TaskStatus.BLOCKED, {
            "out": out, "status": task.status, "error": task.error, "retries": task.retry_count}

        assert task.retry_count == 0 and task.result is None
        assert list(db.scalars(select(models.Event.event_type).where(
            models.Event.task_id == task_id, models.Event.event_type == "TASK_BLOCKED"))) == ["TASK_BLOCKED"]
