"""A parked task opted in with release_on_dependencies wakes when every dependency is DONE."""
import pytest
from sqlalchemy import select

from app import control, enums, models, worker
from tests.test_worker import _db, _task


@pytest.fixture
def db():
    session = _db()
    yield session
    session.close()


def parked(db, key="TASK-0200", deps=(), status=enums.TaskStatus.BACKLOG, opt_in=True, **over):
    task = _task(db, key=key, status=status, **over)
    if opt_in:
        task.context = {**task.context, "release_on_dependencies": True}
    for dep in deps:
        db.add(models.TaskDependency(task_id=task.id, depends_on_id=dep.id))
    db.commit()
    return task


def dep(db, key, status):
    return _task(db, key=key, status=status)


def test_all_dependencies_done_releases_to_ready_with_audit(db):
    a, b = dep(db, "TASK-0201", enums.TaskStatus.DONE), dep(db, "TASK-0202", enums.TaskStatus.DONE)
    task = parked(db, deps=(a, b))
    assert worker.release_dependency_waits(db) == [task.key]
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY
    assert task.context["dependency_release"] == {"depends_on": ["TASK-0201", "TASK-0202"]}
    assert db.scalar(select(models.AuditLog).where(models.AuditLog.action == "task.dependency_release"))
    assert worker.release_dependency_waits(db) == []  # idempotent: no longer parked


@pytest.mark.parametrize("status", [enums.TaskStatus.REVIEW, enums.TaskStatus.IN_PROGRESS,
                                     enums.TaskStatus.READY, enums.TaskStatus.CANCELLED,
                                     enums.TaskStatus.FAILED])
def test_one_unfinished_dependency_keeps_it_parked(db, status):
    task = parked(db, deps=(dep(db, "TASK-0201", enums.TaskStatus.DONE), dep(db, "TASK-0202", status)))
    assert worker.release_dependency_waits(db) == []
    db.refresh(task)
    assert task.status == enums.TaskStatus.BACKLOG


def test_only_explicitly_opted_in_parked_tasks_with_dependencies_move(db):
    done = dep(db, "TASK-0201", enums.TaskStatus.DONE)
    no_opt_in = parked(db, "TASK-0210", deps=(done,), opt_in=False)
    no_deps = parked(db, "TASK-0211")
    string_flag = parked(db, "TASK-0212", deps=(done,))
    string_flag.context = {**string_flag.context, "release_on_dependencies": "true"}
    in_review = parked(db, "TASK-0213", deps=(done,), status=enums.TaskStatus.REVIEW)
    db.commit()
    assert worker.release_dependency_waits(db) == []
    for t, expected in ((no_opt_in, "BACKLOG"), (no_deps, "BACKLOG"), (string_flag, "BACKLOG"),
                        (in_review, "REVIEW")):
        db.refresh(t)
        assert t.status.value == expected, t.key


def test_wakes_external_waits_too_and_never_changes_risk_or_autonomy(db):
    done = dep(db, "TASK-0201", enums.TaskStatus.DONE)
    task = parked(db, deps=(done,), status=enums.TaskStatus.WAITING_EXTERNAL, autonomous=False,
                  approval_required=True)
    assert worker.release_dependency_waits(db) == [task.key]
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.approval_required is True
    assert task.context["autonomous"] is False  # waking is not permission to run unattended
    assert worker._admission_gate(db, task, worker.registry.load())[0] == "not_opted_in"


def test_kill_switch_and_inactive_project_hold_the_release(db):
    done = dep(db, "TASK-0201", enums.TaskStatus.DONE)
    project = models.Project(key="p", name="p", status=enums.ProjectStatus.PAUSED)
    db.add(project)
    db.flush()
    task = parked(db, deps=(done,), project_id=project.id)
    assert worker.release_dependency_waits(db) == []
    project.status = enums.ProjectStatus.ACTIVE
    db.commit()
    control.set_agents_paused(db, True, actor="founder", reason="test")
    assert worker.release_dependency_waits(db) == []
    control.set_agents_paused(db, False, actor="founder", reason="test")
    assert worker.release_dependency_waits(db) == [task.key]
