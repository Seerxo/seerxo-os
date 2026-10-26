"""Deterministic `companyctl run` outer-loop behavior."""
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import enums, main, models


def _task(key, agent, unit):
    return models.Task(
        key=key,
        title=key,
        success_criteria="recorded evidence exists",
        status=enums.TaskStatus.READY,
        priority=enums.Priority.P2,
        risk_level=enums.RiskLevel.LOW,
        assigned_agent_id=agent.id,
        business_unit_id=unit.id,
    )


def test_outer_loop_returns_only_runnable_work_and_calls_no_model():
    # Break caught: `companyctl run` hands inactive work to a model or fails to record
    # the no-work heartbeat for another enabled agent.
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        developer = models.Agent(key="developer", role="Developer", enabled=True)
        research = models.Agent(key="research", role="Research", enabled=True)
        active = models.BusinessUnit(
            key="seerxo", name="Seerxo", status=enums.BusinessUnitStatus.ACTIVE,
            priority=enums.Priority.P1,
        )
        backlog = models.BusinessUnit(
            key="future", name="Future", status=enums.BusinessUnitStatus.BACKLOG,
            priority=enums.Priority.P4,
        )
        db.add_all([developer, research, active, backlog])
        db.flush()
        db.add_all([
            _task("TASK-0001", developer, active),
            _task("TASK-0002", developer, backlog),
        ])
        db.commit()

        result = main.run_outer_loop(db)

        assert result["model_calls"] == 0
        assert result["work_count"] == 1
        assert result["work"][0]["task"] == "TASK-0001"
        assert result["work"][0]["agent"] == "developer"
        assert result["work"][0]["next_command"] == \
            "company-dispatch TASK-0001 --run"
        assert result["skipped"] == [
            {"task": "TASK-0002", "reason": "business_unit_not_active"}
        ]
        assert db.get(models.Agent, developer.id).last_heartbeat_at is not None
        assert db.get(models.Agent, research.id).last_heartbeat_at is not None
        assert db.execute(select(func.count()).select_from(models.AuditLog)).scalar_one() == 1
