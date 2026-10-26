"""Stage B gate golden scenarios: what the unattended worker must never pick up."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import enums, models, registry, worker

SPECS = registry.load()
OK = {"objective": "Echo the token", "success_criteria": "evidence has the token",
      "risk_level": enums.RiskLevel.LOW, "approval_required": False,
      "context": {"autonomous": True, "scope": "company-os"}}


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([models.Agent(key="research", role="r", enabled=True),
                models.Agent(key="ceo", role="c", enabled=False),
                models.BusinessUnit(key="kdp", name="KDP", status=enums.BusinessUnitStatus.BACKLOG,
                                    priority=enums.Priority.P4)])
    db.commit()
    return db


def _reason(**over):
    db = _db()
    agent = db.execute(select(models.Agent).where(models.Agent.key == over.pop("agent", "research"))).scalar_one()
    fields = {**OK, **over}
    if "context" in over and over["context"] is not None:
        fields["context"] = {**OK["context"], **over["context"]}
    unit = fields.pop("unit", None)
    t = models.Task(key="TASK-0600", title="t", status=enums.TaskStatus.READY,
                    priority=enums.Priority.P3, assigned_agent_id=agent.id, **fields)
    if unit:
        t.business_unit_id = db.execute(select(models.BusinessUnit.id)).scalar_one()
    db.add(t)
    db.commit()
    return worker._eligible(db, t, SPECS)[0]


SCENARIOS = [
    ("eligible baseline", {}, None),
    ("not opted in", {"context": {"autonomous": False}}, "not_opted_in"),
    ("opt-in must be literally true", {"context": {"autonomous": "yes"}}, "not_opted_in"),
    ("medium risk", {"risk_level": enums.RiskLevel.MEDIUM}, "needs_human_gate"),
    ("high risk", {"risk_level": enums.RiskLevel.HIGH}, "needs_human_gate"),
    ("critical risk", {"risk_level": enums.RiskLevel.CRITICAL}, "needs_human_gate"),
    ("approval required", {"approval_required": True}, "needs_human_gate"),
    ("code write while Stage C is off", {"context": {"write": True}}, "code_write_disabled"),
    ("vague: no objective", {"objective": ""}, "underspecified:objective"),
    ("vague: thin success criteria", {"success_criteria": "done"}, "underspecified:success_criteria"),
    ("vague: no scope", {"context": {"scope": None}}, "underspecified:scope"),
    ("disabled agent", {"agent": "ceo"}, "agent_disabled"),
    ("backlog business unit gets no work", {"unit": True}, "business_unit_not_active"),
    ("attempts exhausted", {"retry_count": 3, "max_attempts": 3}, "attempts_exhausted"),
]


@pytest.mark.parametrize("name,over,expected", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_gate(name, over, expected):
    assert _reason(**over) == expected
