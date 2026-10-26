"""Open work is unique in the database, not only in a prior SELECT (§150)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import enums, keys, models
from tests.test_event_router import _db, _missing_publish


def _session():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return Session(engine)


def _task(dedupe, status=enums.TaskStatus.READY):
    return models.Task(title="t", success_criteria="s", status=status, priority=enums.Priority.P2,
                       risk_level=enums.RiskLevel.LOW, dedupe_key=dedupe)


def _incident(scope, status=enums.IncidentStatus.OPEN):
    return models.Incident(severity=enums.IncidentSeverity.SEV2, title="i", description="d",
                           started_at=datetime.now(timezone.utc), detected_by="test",
                           status=status, open_scope=scope)


def test_second_open_task_with_same_dedupe_is_rejected_by_the_database():
    db = _session()
    keys.add_with_key(db, _task("job:1"), "TASK")
    db.commit()
    # Break caught: two writers that both passed the SELECT check commit duplicate work.
    with pytest.raises(keys.DuplicateOpenWork):
        keys.add_with_key(db, _task("job:1"), "TASK")
    db.rollback()
    assert db.execute(select(func.count()).select_from(models.Task)).scalar_one() == 1


def test_finished_work_does_not_block_new_work_with_the_same_dedupe():
    db = _session()
    keys.add_with_key(db, _task("job:1", enums.TaskStatus.DONE), "TASK")
    keys.add_with_key(db, _task("job:1"), "TASK")
    keys.add_with_key(db, _task(None), "TASK")
    keys.add_with_key(db, _task(None), "TASK")  # no dedupe key means no uniqueness rule
    db.commit()
    assert db.execute(select(func.count()).select_from(models.Task)).scalar_one() == 4


def test_unresolved_incident_scope_is_unique_but_resolved_ones_are_history():
    db = _session()
    keys.add_with_key(db, _incident("service:1", enums.IncidentStatus.RESOLVED), "INC")
    keys.add_with_key(db, _incident("service:1"), "INC")
    with pytest.raises(keys.DuplicateOpenWork):
        keys.add_with_key(db, _incident("service:1"), "INC")
    db.rollback()
    keys.add_with_key(db, _incident(None), "INC")  # manual incidents carry no scope
    keys.add_with_key(db, _incident(None), "INC")
    db.commit()


def test_a_key_taken_by_a_concurrent_writer_is_retried(monkeypatch):
    db = _session()
    keys.add_with_key(db, _task(None), "TASK")
    db.commit()
    real, calls = keys.next_key, []

    def stale_then_real(*args, **kwargs):
        calls.append(1)
        # First pick is the key another writer already committed (stale count).
        return "TASK-0001" if len(calls) == 1 else real(*args, **kwargs)

    monkeypatch.setattr(keys, "next_key", stale_then_real)
    task = keys.add_with_key(db, _task(None), "TASK")
    db.commit()
    assert task.key == "TASK-0002" and len(calls) == 2


def test_other_integrity_errors_are_not_mistaken_for_key_collisions():
    db = _session()
    bad = _task(None)
    bad.title = None  # NOT NULL violation must surface, not loop
    with pytest.raises(IntegrityError):
        keys.add_with_key(db, bad, "TASK")


def test_router_returns_the_incident_a_concurrent_event_opened():
    db = _db()
    svc = db.execute(select(models.Service)).scalar_one()
    # Committed by a concurrent writer after this router's SELECT: same scope, other title.
    db.add(models.Incident(key="INC-0007", severity=enums.IncidentSeverity.SEV1, title="other",
                           description="x", started_at=svc.created_at, detected_by="watchdog",
                           open_scope=f"service:{svc.id}"))
    db.commit()
    # The router's SELECT matches on service_id, so it misses this row: only the index catches it.
    _missing_publish(db)
    assert db.execute(select(func.count()).select_from(models.Incident)).scalar_one() == 1
    assert db.execute(select(func.count()).select_from(models.Task)).scalar_one() == 0
