"""Supervisor golden scenarios (§45). Each row is a claim a worker might make."""
import pytest

from app import enums, models, worker

TASK = models.Task(key="TASK-0500", title="t", success_criteria="evidence has tok-1",
                   context={"verify": {"must_contain": ["tok-1", "TASK-0500"]}})
V = enums.SupervisorVerdict


def result(**over):
    body = {"task_id": "TASK-0500", "agent_id": "research", "status": "completed",
            "summary": "echoed tok-1 for TASK-0500", "actions": [],
            "evidence": ["tok-1", "TASK-0500"], "artifacts": [], "learning_candidates": [],
            "new_task_candidates": [], "approval_request": None, "error": None}
    body.update(over)
    return body


SCENARIOS = [
    ("verified result is approved", result(), V.APPROVED),
    ("'done' with no evidence", result(summary="Done.", evidence=[]), V.REVISION_REQUIRED),
    ("evidence missing required token", result(evidence=["TASK-0500"], summary="ok"), V.REVISION_REQUIRED),
    ("result for another task", result(task_id="TASK-0001"), V.FAILED),
    ("malformed: missing fields", {"status": "completed"}, V.FAILED),
    ("malformed: unknown status", result(status="success"), V.FAILED),
    ("malformed: empty summary", result(summary=""), V.FAILED),
    ("not an object", None, V.FAILED),
    ("worker reports failure", result(status="failed", error="could not"), V.FAILED),
    ("worker asks for approval", result(status="needs_approval", approval_request="deploy"), V.ESCALATE),
    ("worker blocked", result(status="blocked"), V.ESCALATE),
    ("'completed' smuggling an approval request",
     result(approval_request="deploy production now"), V.ESCALATE),
    ("tests-passed claim without the required token",
     result(summary="All tests passed, task complete", evidence=["pytest: 120 passed"]),
     V.REVISION_REQUIRED),
]


@pytest.mark.parametrize("name,raw,expected", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_supervisor(name, raw, expected):
    verdict, _, _ = worker.supervise(TASK, raw)
    assert verdict == expected
