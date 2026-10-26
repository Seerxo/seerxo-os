"""Minimal task context: relevant state in, company-wide noise out."""
import copy
import importlib
import importlib.util

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import enums, models, registry


def test_context_packet_excludes_unrelated_state_and_caps_required_files():
    # Break caught: a worker receives another project's decisions, notes or runs, or
    # expands an unbounded file list into its context window.
    assert importlib.util.find_spec("app.context_builder") is not None, \
        "context_builder feature missing"
    context_builder = importlib.import_module("app.context_builder")

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        agent = models.Agent(key="developer", role="Developer", enabled=True)
        unit = models.BusinessUnit(
            key="seerxo", name="Seerxo", status=enums.BusinessUnitStatus.ACTIVE,
            priority=enums.Priority.P1,
        )
        db.add_all([agent, unit])
        db.flush()
        project = models.Project(
            key="seerxo-api", name="Seerxo API", business_unit_id=unit.id,
            status=enums.ProjectStatus.ACTIVE, priority=enums.Priority.P1,
            current_phase="maintenance",
        )
        unrelated = models.Project(
            key="other", name="Other", business_unit_id=unit.id,
            status=enums.ProjectStatus.ACTIVE, priority=enums.Priority.P3,
        )
        db.add_all([project, unrelated])
        db.flush()
        task = models.Task(
            key="TASK-1000", title="Fix API", success_criteria="tests pass",
            status=enums.TaskStatus.READY, priority=enums.Priority.P1,
            risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id,
            business_unit_id=unit.id, project_id=project.id,
            context={
                "required_files": ["app/a.py", "app/b.py", "app/c.py"],
                "required_tool_state": ["git_status"],
            },
        )
        related_run_task = models.Task(
            key="TASK-0999", title="Earlier API work", success_criteria="verified",
            status=enums.TaskStatus.DONE, priority=enums.Priority.P2,
            risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id,
            business_unit_id=unit.id, project_id=project.id,
        )
        unrelated_run_task = models.Task(
            key="TASK-0998", title="Other work", success_criteria="verified",
            status=enums.TaskStatus.DONE, priority=enums.Priority.P3,
            risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id,
            business_unit_id=unit.id, project_id=unrelated.id,
        )
        db.add_all([task, related_run_task, unrelated_run_task])
        db.flush()
        db.add_all([
            models.Decision(
                key="DEC-1000", title="Relevant", decision="Keep API",
                reason="Revenue", project_id=project.id,
                status=enums.DecisionStatus.ACTIVE,
            ),
            models.Decision(
                key="DEC-1001", title="Unrelated", decision="Ignore",
                reason="Other project", project_id=unrelated.id,
                status=enums.DecisionStatus.ACTIVE,
            ),
            models.Learning(
                title="Relevant learning", lesson="Run migration first",
                project_id=project.id, status=enums.LearningStatus.APPROVED,
            ),
            models.Learning(
                title="Unrelated learning", lesson="Other lesson",
                project_id=unrelated.id, status=enums.LearningStatus.APPROVED,
            ),
            models.AgentNote(
                agent_id=agent.id, project_id=project.id, task_id=related_run_task.id,
                note="Relevant note", fingerprint="relevant",
            ),
            models.AgentNote(
                agent_id=agent.id, project_id=unrelated.id, task_id=unrelated_run_task.id,
                note="Unrelated note", fingerprint="unrelated",
            ),
            models.AgentRun(
                agent_id=agent.id, task_id=related_run_task.id,
                status=enums.AgentRunStatus.SUCCEEDED, result="related result",
            ),
            models.AgentRun(
                agent_id=agent.id, task_id=unrelated_run_task.id,
                status=enums.AgentRunStatus.SUCCEEDED, result="unrelated result",
            ),
        ])
        db.commit()

        spec = copy.deepcopy(registry.load()["developer"])
        spec.context_policy["max_files"] = 2
        packet = context_builder.build(db, task, spec)

        assert packet["task"]["key"] == "TASK-1000"
        assert packet["agent"]["key"] == "developer"
        assert packet["project"]["key"] == "seerxo-api"
        assert [item["key"] for item in packet["decisions"]] == ["DEC-1000"]
        assert [item["title"] for item in packet["learnings"]] == ["Relevant learning"]
        assert [item["note"] for item in packet["agent_notes"]] == ["Relevant note"]
        assert [item["task"] for item in packet["recent_runs"]] == ["TASK-0999"]
        assert packet["required_files"] == ["app/a.py", "app/b.py"]
        assert packet["required_tool_state"] == ["git_status"]
        assert packet["truncated_files"] == 1


def test_the_facts_that_opened_a_task_reach_the_agent():
    # Break caught (2026-10-24): the reconciler wrote the goal's measured MRR, baseline and target into the
    # task's context, but the packet dropped it; three planning runs then asked for the MRR to be "located".
    from app import context_builder

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        agent = models.Agent(key="ceo", role="CEO", enabled=True)
        db.add(agent)
        db.flush()
        evidence = {"rule": "GOAL_NO_WORK", "goal_id": 5, "value": 39.98, "unit": "USD", "baseline": 39.98,
                    "target": 200.0, "source": "polar-api", "measurement_status": "VERIFIED"}
        task = models.Task(key="TASK-0084", title="Goal 5 has no work: plan the next step", success_criteria="plan",
                           status=enums.TaskStatus.READY, priority=enums.Priority.P1,
                           risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id,
                           context={"goal": 5, "rule": "GOAL_NO_WORK", "evidence": evidence})
        plain = models.Task(key="TASK-0001", title="No trigger", success_criteria="x",
                            status=enums.TaskStatus.READY, priority=enums.Priority.P3,
                            risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id,
                            context={"evidence": "not a dict"})
        huge = models.Task(key="TASK-0002", title="Flood", success_criteria="x",
                           status=enums.TaskStatus.READY, priority=enums.Priority.P3,
                           risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id,
                           context={"evidence": {"blob": "x" * 10_000}})
        db.add_all([task, plain, huge])
        db.flush()
        spec = copy.deepcopy(registry.load()["ceo"])
        assert context_builder.build(db, task, spec)["trigger_evidence"] == evidence
        assert context_builder.build(db, plain, spec)["trigger_evidence"] is None
        assert "omitted" in context_builder.build(db, huge, spec)["trigger_evidence"]
