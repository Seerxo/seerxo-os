"""herdr run state -> Company OS vocabulary.

The invariant worth protecting above all others: this mapping can never produce DONE.
The control center approves *code*; only the founder closes a *task* (§45).
"""
import pytest

from app import enums, runreport


def _state(**kw) -> dict:
    base = {"state": "running", "workflow": {"name": "standard"},
            "created_at": "2026-09-30T10:00:00Z", "goal": "do the thing"}
    review = kw.pop("verdict", None)
    checks = kw.pop("checks", None)
    if review is not None:
        base["review"] = {"verdict": review}
    if checks is not None:
        base["checks"] = {"status": checks}
    base.update(kw)
    return base


# --- the invariant ---------------------------------------------------------------

@pytest.mark.parametrize("st", [
    _state(state="finished", verdict="PASS", checks="passed"),
    _state(state="finished", checks="passed"),
    _state(state="finished"),
    _state(state="blocked"),
    _state(state="cancelled"),
    _state(verdict="REQUEST_CHANGES"),
    _state(checks="failed"),
    _state(),
])
def test_never_produces_done(st):
    assert runreport.interpret(st).task_status is not enums.TaskStatus.DONE


# --- the happy path --------------------------------------------------------------

def test_passed_review_reaches_review_not_done():
    out = runreport.interpret(_state(state="finished", verdict="PASS", checks="passed"))
    assert out.run_status is enums.AgentRunStatus.SUCCEEDED
    assert out.supervisor_verdict is enums.SupervisorVerdict.APPROVED
    assert out.task_status is enums.TaskStatus.REVIEW
    assert out.event_type == "TASK_COMPLETED"


def test_quick_workflow_without_a_reviewer_still_completes():
    # `quick` has no reviewer by design; an empty verdict is expected, not an error.
    out = runreport.interpret(_state(state="finished", checks="passed",
                                     workflow={"name": "quick"}))
    assert out.run_status is enums.AgentRunStatus.SUCCEEDED
    assert out.task_status is enums.TaskStatus.REVIEW


def test_finished_without_checks_or_review_is_not_approved():
    out = runreport.interpret(_state(state="finished"))
    assert out.supervisor_verdict is None
    assert "without a recorded review" in out.summary


# --- failure and escalation ------------------------------------------------------

def test_blocked_escalates_and_outranks_a_passing_check():
    out = runreport.interpret(_state(state="blocked", checks="passed", verdict="PASS"))
    assert out.run_status is enums.AgentRunStatus.ESCALATED
    assert out.supervisor_verdict is enums.SupervisorVerdict.ESCALATE
    assert out.task_status is enums.TaskStatus.BLOCKED


def test_blocked_verdict_escalates_even_when_state_looks_finished():
    out = runreport.interpret(_state(state="finished", verdict="BLOCKED"))
    assert out.task_status is enums.TaskStatus.BLOCKED


def test_failed_checks_keep_the_task_in_progress():
    out = runreport.interpret(_state(checks="failed"))
    assert out.run_status is enums.AgentRunStatus.FAILED
    assert out.supervisor_verdict is enums.SupervisorVerdict.FAILED
    assert out.task_status is enums.TaskStatus.IN_PROGRESS


def test_request_changes_is_a_working_supervisor_not_a_failed_run():
    out = runreport.interpret(_state(verdict="REQUEST_CHANGES", checks="passed"))
    assert out.run_status is enums.AgentRunStatus.SUCCEEDED
    assert out.supervisor_verdict is enums.SupervisorVerdict.REVISION_REQUIRED
    assert out.task_status is enums.TaskStatus.IN_PROGRESS


def test_cancelled_returns_the_task_to_the_queue():
    # Cancelling is not failing: the work was never judged, so the task goes back READY.
    out = runreport.interpret(_state(state="cancelled"))
    assert out.task_status is enums.TaskStatus.READY
    assert out.severity is enums.Severity.LOW


def test_running_concludes_nothing():
    out = runreport.interpret(_state(state="running"))
    assert out.run_status is enums.AgentRunStatus.STARTED
    assert out.supervisor_verdict is None


# --- robustness against a changed upstream ---------------------------------------

def test_missing_fields_do_not_raise():
    out = runreport.interpret({})
    assert out.run_status is enums.AgentRunStatus.STARTED


def test_unknown_state_is_treated_as_in_progress_not_success():
    out = runreport.interpret(_state(state="some-future-state"))
    assert out.task_status is enums.TaskStatus.IN_PROGRESS
    assert out.run_status is not enums.AgentRunStatus.SUCCEEDED


def test_verdict_case_is_normalised():
    assert runreport.interpret(_state(state="finished", verdict="pass")).supervisor_verdict \
        is enums.SupervisorVerdict.APPROVED


def test_terminal_detection_matches_metrics_mjs():
    for s in ("finished", "cancelled", "blocked"):
        assert runreport.is_terminal({"state": s})
    assert not runreport.is_terminal({"state": "running"})
    assert not runreport.is_terminal({})


def test_every_outcome_event_type_is_alertable_or_deliberately_quiet():
    # TASK_STARTED and TASK_COMPLETED are intentionally NOT alertable: §111 forbids
    # paging the founder for routine success.
    quiet = {"TASK_STARTED", "TASK_COMPLETED", "TASK_FAILED"}
    for st in (_state(), _state(state="finished", verdict="PASS"), _state(checks="failed")):
        assert runreport.interpret(st).event_type in quiet
