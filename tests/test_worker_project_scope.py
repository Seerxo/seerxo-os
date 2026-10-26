"""Read-only execution still respects project and business-unit permissions."""
import pytest

from app import enums, models, worker
from tests.test_worker import SPECS, FakeProvider, _db, _good, _task


@pytest.mark.parametrize("status", [s for s in enums.ProjectStatus if s != enums.ProjectStatus.ACTIVE])
def test_an_inactive_project_never_consumes_a_provider_call(status):
    db = _db()
    project = models.Project(key="paused-project", name="Paused project", status=status)
    db.add(project)
    db.commit()
    task = _task(db, project_id=project.id)
    provider = FakeProvider("claude", [_good(task.key)])

    outcome = worker.run_once(db, {"claude": provider}, SPECS)

    assert provider.calls == 0
    assert outcome["model_calls"] == 0
    assert outcome["skipped"] == [{"task": task.key, "reason": "project_not_active"}]
    assert task.status == enums.TaskStatus.READY


def test_a_project_cannot_hide_its_inactive_business_unit_by_omitting_the_task_unit():
    db = _db()
    unit = models.BusinessUnit(key="paused-unit", name="Paused unit", status=enums.BusinessUnitStatus.PAUSED)
    project = models.Project(key="active-project", name="Active project", status=enums.ProjectStatus.ACTIVE,
                             business_unit=unit)
    db.add(project)
    db.commit()
    task = _task(db, project_id=project.id, business_unit_id=None)
    provider = FakeProvider("claude", [_good(task.key)])

    outcome = worker.run_once(db, {"claude": provider}, SPECS)

    assert provider.calls == 0
    assert outcome["skipped"] == [{"task": task.key, "reason": "business_unit_not_active"}]


def test_a_task_cannot_borrow_another_active_units_permission():
    db = _db()
    project_unit = models.BusinessUnit(key="project-unit", name="Project unit",
                                      status=enums.BusinessUnitStatus.ACTIVE)
    task_unit = models.BusinessUnit(key="task-unit", name="Task unit", status=enums.BusinessUnitStatus.ACTIVE)
    project = models.Project(key="active-project", name="Active project", status=enums.ProjectStatus.ACTIVE,
                             business_unit=project_unit)
    db.add_all([project, task_unit])
    db.commit()
    task = _task(db, project_id=project.id, business_unit_id=task_unit.id)

    reason, _ = worker._admission_gate(db, task, SPECS)

    assert reason == "business_unit_mismatch"


@pytest.mark.parametrize("field,reason", [("project_id", "project_missing"),
                                         ("business_unit_id", "business_unit_missing")])
@pytest.mark.parametrize("scope_id", [0, -1, 999999])
def test_unknown_scope_id_fails_closed(field, reason, scope_id):
    db = _db()
    task = _task(db, **{field: scope_id})

    actual, _ = worker._admission_gate(db, task, SPECS)

    assert actual == reason


@pytest.mark.parametrize("model,field,status,reason", [
    (models.Project, "project_id", enums.ProjectStatus.PAUSED, "project_not_active"),
    (models.BusinessUnit, "business_unit_id", enums.BusinessUnitStatus.PAUSED, "business_unit_not_active"),
])
def test_zero_id_does_not_hide_a_persisted_inactive_scope(model, field, status, reason):
    db = _db()
    db.add(model(id=0, key="zero", name="Zero", status=status))
    db.commit()
    task = _task(db, **{field: 0})

    assert worker._admission_gate(db, task, SPECS)[0] == reason


def test_active_project_work_and_explicit_company_work_still_execute():
    db = _db()
    project = models.Project(key="active-project", name="Active project", status=enums.ProjectStatus.ACTIVE)
    db.add(project)
    db.commit()
    project_task = _task(db, key="TASK-8100", project_id=project.id)
    company_task = _task(db, key="TASK-8101")
    provider = FakeProvider("claude", [_good(project_task.key), _good(company_task.key)])

    assert worker.run_once(db, {"claude": provider}, SPECS)["task"] == project_task.key
    assert worker.run_once(db, {"claude": provider}, SPECS)["task"] == company_task.key
    assert provider.calls == 2
