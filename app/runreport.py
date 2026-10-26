"""Bring a herdr control-center run back into Company OS (docs/AGENT_EXECUTION.md §4.2).

The control center's review **is** the supervisor of §45. This module does not form its
own opinion about whether work succeeded — it translates the verdict that already
exists. Inventing a verdict here would recreate the self-approval problem §45 forbids,
one layer up.

Input is the control center's own `state.json`, whose shape is fixed by
`src/task.mjs`: state, workflow{name,risk}, checks{status}, review{verdict},
created_at/updated_at/finished_at/cancelled_at, checkout, branch, source_commit.
"""
from dataclasses import asdict, dataclass

from app import enums

# herdr task states. Terminal set matches metrics.mjs.
TERMINAL = {"finished", "cancelled", "blocked"}


@dataclass
class RunOutcome:
    run_status: enums.AgentRunStatus
    supervisor_verdict: enums.SupervisorVerdict | None
    task_status: enums.TaskStatus
    event_type: str
    severity: enums.Severity
    summary: str

    def as_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if hasattr(v, "value"):
                d[k] = v.value
        return d


def interpret(state: dict) -> RunOutcome:
    """Map a control-center task state onto Company OS vocabulary.

    Never returns TaskStatus.DONE. Only the founder closes a task (§45), and this
    function has no way to be the founder.
    """
    herdr_state = (state.get("state") or "unknown").lower()
    verdict = ((state.get("review") or {}).get("verdict") or "").upper()
    checks = ((state.get("checks") or {}).get("status") or "").lower()
    workflow = (state.get("workflow") or {}).get("name") or "unknown"

    # Blocked beats everything: the control center stopped and wants a human.
    if herdr_state == "blocked" or verdict == "BLOCKED":
        return RunOutcome(
            enums.AgentRunStatus.ESCALATED, enums.SupervisorVerdict.ESCALATE,
            enums.TaskStatus.BLOCKED, "TASK_FAILED_REPEATEDLY", enums.Severity.HIGH,
            f"{workflow}: blocked — the control center stopped and needs a human",
        )

    if herdr_state == "cancelled":
        return RunOutcome(
            enums.AgentRunStatus.FAILED, None,
            # Cancelled is not failed work; the task returns to the queue untouched.
            enums.TaskStatus.READY, "TASK_FAILED", enums.Severity.LOW,
            f"{workflow}: run cancelled before completion",
        )

    if checks == "failed":
        return RunOutcome(
            enums.AgentRunStatus.FAILED, enums.SupervisorVerdict.FAILED,
            enums.TaskStatus.IN_PROGRESS, "TASK_FAILED", enums.Severity.MEDIUM,
            f"{workflow}: the configured quality gate failed",
        )

    if verdict == "REQUEST_CHANGES":
        return RunOutcome(
            # The run itself completed; the reviewer wants another pass. That is a
            # working supervisor, not a failure.
            enums.AgentRunStatus.SUCCEEDED, enums.SupervisorVerdict.REVISION_REQUIRED,
            enums.TaskStatus.IN_PROGRESS, "TASK_FAILED", enums.Severity.LOW,
            f"{workflow}: reviewer requested changes",
        )

    if herdr_state == "finished" and verdict == "PASS":
        return RunOutcome(
            enums.AgentRunStatus.SUCCEEDED, enums.SupervisorVerdict.APPROVED,
            # REVIEW, never DONE — the reviewer approved the code, the founder still
            # decides whether the business task is complete (§45).
            enums.TaskStatus.REVIEW, "TASK_COMPLETED", enums.Severity.INFO,
            f"{workflow}: checks passed and review approved — awaiting founder",
        )

    if herdr_state == "finished":
        # `quick` has no reviewer by design, so an empty verdict here is expected.
        return RunOutcome(
            enums.AgentRunStatus.SUCCEEDED,
            enums.SupervisorVerdict.APPROVED if checks == "passed" else None,
            enums.TaskStatus.REVIEW, "TASK_COMPLETED", enums.Severity.INFO,
            f"{workflow}: finished"
            + (" with checks passed" if checks == "passed" else " without a recorded review"),
        )

    # Still running. Recorded so the run exists in history, but nothing is concluded.
    return RunOutcome(
        enums.AgentRunStatus.STARTED, None,
        enums.TaskStatus.IN_PROGRESS, "TASK_STARTED", enums.Severity.INFO,
        f"{workflow}: in progress ({herdr_state})",
    )


def is_terminal(state: dict) -> bool:
    return (state.get("state") or "").lower() in TERMINAL
