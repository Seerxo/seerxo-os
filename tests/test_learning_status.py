"""§53A: a promoted notebook note is a candidate, and only approved learnings reach agents."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import context_builder, enums, main, models, notebook, registry, schemas


from types import SimpleNamespace
from app import approval_identity
_FOUNDER_REQUEST = SimpleNamespace(state=SimpleNamespace(principal=approval_identity.founder("founder@example.invalid")))


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    agent = models.Agent(key="developer", role="Developer", enabled=True)
    unit = models.BusinessUnit(key="seerxo", name="Seerxo", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1)
    db.add_all([agent, unit])
    db.flush()
    task = models.Task(key="TASK-2000", title="Fix", success_criteria="ok", status=enums.TaskStatus.READY,
                       priority=enums.Priority.P1, risk_level=enums.RiskLevel.LOW,
                       assigned_agent_id=agent.id, business_unit_id=unit.id)
    db.add(task)
    db.commit()
    return db, task, unit


def _lessons(db, task):
    spec = registry.load()["developer"]
    return [x["lesson"] for x in context_builder.build(db, task, spec)["learnings"]]


def _promote_recurring_note(db, unit):
    now = datetime.now(timezone.utc)
    db.add(models.AgentNote(note="retry the flaky upload once", fingerprint="fp-upload", reference_count=3,
                            created_at=now - timedelta(days=3), last_seen_at=now))
    db.commit()
    notebook.apply_compaction(db, notebook.plan_compaction(db))
    learning = db.execute(select(models.Learning)).scalar_one()
    learning.business_unit_id = unit.id  # scope it to the task so only status can exclude it
    db.commit()
    return learning


def test_a_promoted_note_is_a_candidate_and_is_not_given_to_agents():
    db, task, unit = _db()
    learning = _promote_recurring_note(db, unit)
    assert learning.status == enums.LearningStatus.CANDIDATE
    # Break caught: an unreviewed candidate is served to agents as trusted memory.
    assert _lessons(db, task) == []


def test_founder_approval_promotes_a_candidate_once_and_is_audited():
    db, task, unit = _db()
    learning = _promote_recurring_note(db, unit)
    out = main.approve_learning(learning.id, schemas.LearningApprove(actor="founder", reason="seen it"), db, _FOUNDER_REQUEST)
    assert out["status"] == "APPROVED" and _lessons(db, task) == ["retry the flaky upload once"]
    assert db.query(models.AuditLog).filter_by(action="learning.approve").count() == 1
    with pytest.raises(HTTPException) as e:
        main.approve_learning(learning.id, schemas.LearningApprove(actor="founder"), db, _FOUNDER_REQUEST)
    assert e.value.status_code == 409


def test_a_learning_anyone_records_is_a_candidate_until_the_verified_founder_approves_it():
    db, task, unit = _db()
    created = main.create_learning(schemas.LearningCreate(title="t", lesson="check DNS first",
                                                          business_unit="seerxo"), db)
    assert _lessons(db, task) == [] and main.list_learnings(db)[0]["status"] == "CANDIDATE"
    main.approve_learning(created["id"], schemas.LearningApprove(actor="founder"), db, _FOUNDER_REQUEST)
    assert _lessons(db, task) == ["check DNS first"]


def test_omitting_status_cannot_create_trusted_memory():
    db, _, _ = _db()
    row = models.Learning(title="unreviewed", lesson="never trust omitted defaults")
    db.add(row)
    db.commit()
    assert row.status == enums.LearningStatus.CANDIDATE
