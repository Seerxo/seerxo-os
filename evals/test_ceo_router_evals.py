"""CEO-side guards that exist as code today. Model-judgement evals for the CEO wait
until the CEO has a scheduled run (AUTONOMOUS_AGENT_ARCHITECTURE.md W6)."""
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import dispatch, enums, models, watchdog


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    unit = models.BusinessUnit(key="youtube", name="YT", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P0)
    db.add_all([unit, models.Agent(key="developer", role="d", enabled=True)])
    db.flush()
    db.add(models.Service(key="youtube-auto", name="YT", status=enums.ServiceStatus.ACTIVE,
                          risk=enums.RiskLevel.HIGH, business_unit_id=unit.id))
    db.commit()
    return db


def test_production_incident_becomes_p0_work_and_repeats_create_no_duplicates():
    db = _db()
    finding = watchdog.Finding("SCHEDULED_PRODUCTION_MISSING", enums.Severity.HIGH, "no publish",
                               "48h", service_key="youtube-auto", dedupe="yt")
    for _ in range(5):
        watchdog.emit(db, [finding])
    tasks = db.execute(select(models.Task)).scalars().all()
    assert len(tasks) == 1 and tasks[0].priority == enums.Priority.P0
    assert tasks[0].context["autonomous"] is False


def test_inactive_business_unit_gets_no_dispatch():
    unit = models.BusinessUnit(key="kdp", name="KDP", status=enums.BusinessUnitStatus.BACKLOG,
                               priority=enums.Priority.P4)
    agent = models.Agent(key="developer", role="d", enabled=True)
    task = models.Task(key="T", title="t", success_criteria="x", status=enums.TaskStatus.READY)
    assert dispatch.skip_reason(task, agent, unit, []) == "business_unit_not_active"


def test_no_business_signal_means_no_work_and_no_model_call():
    from app import registry, worker
    db = _db()
    out = worker.run_once(db, {}, registry.load())
    assert out["model_calls"] == 0 and out["work"] is None
    assert db.execute(select(func.count()).select_from(models.Task)).scalar_one() == 0
