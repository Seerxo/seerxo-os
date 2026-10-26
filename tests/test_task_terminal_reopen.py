"""The HTTP status route requires the verified founder to reopen terminal/review tasks."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cloudflare_access, enums, main, models

ADMIN, HOST, FOUNDER = "a" * 40, "desk.example.invalid", "founder@example.invalid"


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        for key, st in (("TASK-0001", enums.TaskStatus.DONE), ("TASK-0002", enums.TaskStatus.CANCELLED),
                        ("TASK-0003", enums.TaskStatus.IN_PROGRESS), ("TASK-0004", enums.TaskStatus.REVIEW)):
            s.add(models.Task(key=key, title="t", success_criteria="s", status=st,
                              result="original" if st == enums.TaskStatus.REVIEW else None))
        s.add(models.Task(key="TASK-0005", title="dead letter", success_criteria="s",
                          status=enums.TaskStatus.FAILED, retry_count=3, max_attempts=3))
        s.commit()
    for k, v in {"COMPANY_API_TOKEN_ADMIN": ADMIN, "COMPANY_API_TOKEN_DESK": "d" * 40, "COMPANY_API_TOKEN_YOUTUBE": "y" * 40,
                 "API_HOST_IPS": "10.0.6.1", "CF_ACCESS_DESK_HOST": HOST, "API_AUTH_MODE": "enforce"}.items():
        monkeypatch.setenv(k, v)

    async def identity(token, **kw):
        return FOUNDER if token == "founder" else None
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)

    def get_db():
        with Session(engine) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db
    yield engine
    main.app.dependency_overrides.clear()


def _status(engine, key):
    with Session(engine) as s:
        return s.scalar(select(models.Task.status).where(models.Task.key == key))


def _post(key, status, founder=False):
    c = TestClient(main.app, base_url=f"https://{HOST}" if founder else "http://testserver", client=("172.18.0.5", 50000))
    return c.post(f"/tasks/{key}/status", json={"status": status},
                  headers={"Cf-Access-Jwt-Assertion": "founder"} if founder else {"authorization": f"Bearer {ADMIN}"})


@pytest.mark.parametrize("key,was", [("TASK-0001", enums.TaskStatus.DONE), ("TASK-0002", enums.TaskStatus.CANCELLED)])
def test_operator_cannot_reopen_a_closed_task_but_the_founder_can(engine, key, was):
    assert _post(key, "READY").status_code == 403 and _status(engine, key) == was
    assert _post(key, "READY", founder=True).status_code == 200 and _status(engine, key) == enums.TaskStatus.READY


def test_operator_still_moves_open_tasks_and_a_same_status_update_is_allowed(engine):
    assert _post("TASK-0003", "READY").status_code == 200
    assert _post("TASK-0001", "DONE").status_code in (401, 403)  # DONE stays founder-only


def test_only_the_verified_founder_can_reopen_a_dead_letter_task(engine):
    assert _post("TASK-0005", "READY").status_code == 403
    assert _status(engine, "TASK-0005") == enums.TaskStatus.FAILED
    assert _post("TASK-0005", "READY", founder=True).status_code == 200
    assert _status(engine, "TASK-0005") == enums.TaskStatus.READY
    with Session(engine) as s:
        row = s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0005"))
        assert row.actor == f"founder:access:{FOUNDER}"


@pytest.mark.parametrize("status", ["CANCELLED", "FAILED"])
def test_only_the_verified_founder_can_close_an_open_task(status, engine):
    response = _post("TASK-0003", status)
    assert response.status_code == 403
    assert _status(engine, "TASK-0003") == enums.TaskStatus.IN_PROGRESS
    with Session(engine) as s:
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0003")) is None

    response = _post("TASK-0003", status, founder=True)
    assert response.status_code == 200
    assert _status(engine, "TASK-0003") == enums.TaskStatus(status)
    with Session(engine) as s:
        row = s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0003"))
        assert row.actor == f"founder:access:{FOUNDER}"


def test_operator_cannot_use_an_intermediate_state_to_forge_review(engine):
    assert _post("TASK-0003", "IN_PROGRESS").status_code == 200
    assert _post("TASK-0003", "REVIEW").status_code == 403
    assert _status(engine, "TASK-0003") == enums.TaskStatus.IN_PROGRESS


def test_operator_cannot_leave_review_and_overwrite_its_result(engine):
    c = TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000))
    body = {"status": "IN_PROGRESS", "result": "forged", "actor": "founder"}
    r = c.post("/tasks/TASK-0004/status", json=body, headers={"authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 403
    with Session(engine) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0004"))
        assert task.status == enums.TaskStatus.REVIEW and task.result == "original"
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == task.key)) is None
    founder = TestClient(main.app, base_url=f"https://{HOST}", client=("172.18.0.5", 50000))
    assert founder.post("/tasks/TASK-0004/status", json={"status": "IN_PROGRESS", "result": "approved revision"},
                        headers={"Cf-Access-Jwt-Assertion": "founder"}).status_code == 200


def test_operator_cannot_overwrite_cancelled_result_with_same_status(engine):
    c = TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000))
    r = c.post("/tasks/TASK-0002/status", json={"status": "CANCELLED", "result": "forged"},
               headers={"authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 403
    with Session(engine) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0002"))
        assert task.status == enums.TaskStatus.CANCELLED and task.result is None
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.entity_key == task.key)) is None
