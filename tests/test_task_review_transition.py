"""Manual REVIEW cannot be forged by changing a task's previous status."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, main, models
from tests.test_agent_lifecycle_postgres import FOUNDER_H, OPERATOR_H, _api, _task, engine


@pytest.mark.parametrize("start", [
    enums.TaskStatus.IDEA, enums.TaskStatus.BACKLOG, enums.TaskStatus.READY,
    enums.TaskStatus.QUEUED, enums.TaskStatus.IN_PROGRESS, enums.TaskStatus.BLOCKED,
    enums.TaskStatus.WAITING_EXTERNAL, enums.TaskStatus.WAITING_APPROVAL,
    enums.TaskStatus.REVIEW, enums.TaskStatus.FAILED,
])
def test_manual_review_requires_verified_founder_from_every_open_state(engine, start):
    key = _task(engine, status=start)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        task.result = "original" if start == enums.TaskStatus.REVIEW else None
        db.commit()

    body = {"status": "REVIEW", "result": "forged", "actor": "founder"}
    callers = [(_api("http://testserver"), OPERATOR_H, 403),
               (TestClient(main.app, client=("127.0.0.1", 50000)), {}, 403),
               (_api("http://testserver"), {}, 401)]
    for client, headers, code in callers:
        assert client.post(f"/tasks/{key}/status", json=body, headers=headers).status_code == code
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert task.status == start and task.result == ("original" if start == enums.TaskStatus.REVIEW else None)
        assert db.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == key)).first() is None

    response = _api().post(f"/tasks/{key}/status", json={"status": "REVIEW", "result": "verified"}, headers=FOUNDER_H)
    assert response.status_code == 200
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        audit = db.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == key))
        assert task.status == enums.TaskStatus.REVIEW and task.result == "verified"
        assert audit.actor.startswith("founder:access:")


@pytest.mark.parametrize("start,target", [
    (enums.TaskStatus.REVIEW, enums.TaskStatus.IN_PROGRESS),
    (enums.TaskStatus.CANCELLED, enums.TaskStatus.CANCELLED),
])
def test_protected_result_cannot_change_without_founder(engine, start, target):
    key = _task(engine, status=start)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        task.result = "original"
        db.commit()

    body = {"status": target.value, "result": "forged", "actor": "founder"}
    callers = [(_api("http://testserver"), OPERATOR_H, 403),
               (TestClient(main.app, client=("127.0.0.1", 50000)), {}, 403)]
    for client, headers, code in callers:
        assert client.post(f"/tasks/{key}/status", json=body, headers=headers).status_code == code
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert task.status == start and task.result == "original"
        assert db.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == key)).first() is None

    r = _api().post(f"/tasks/{key}/status", json={"status": target.value, "result": "approved revision"}, headers=FOUNDER_H)
    assert r.status_code == 200
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        audit = db.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == key))
        assert task.status == target and task.result == "approved revision"
        assert audit.actor.startswith("founder:access:")
