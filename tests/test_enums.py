"""The constitution fixes these vocabularies; §32 forbids inventing states."""
from app import enums


def test_task_status_has_exactly_the_twelve_constitutional_states():
    assert {s.value for s in enums.TaskStatus} == {
        "IDEA", "BACKLOG", "READY", "QUEUED", "IN_PROGRESS", "WAITING_EXTERNAL",
        "WAITING_APPROVAL", "BLOCKED", "REVIEW", "FAILED", "DONE", "CANCELLED",
    }


def test_open_states_exclude_terminal_ones():
    open_values = {s.value for s in enums.OPEN_TASK_STATES}
    for terminal in ("DONE", "CANCELLED", "FAILED", "IDEA", "BACKLOG"):
        assert terminal not in open_values


def test_priorities_are_p0_to_p4():
    assert [p.value for p in enums.Priority] == ["P0", "P1", "P2", "P3", "P4"]


def test_alertable_events_stay_a_small_allowlist():
    # §111: growing this set is how notification spam starts, so the size is asserted.
    assert len(enums.ALERTABLE_EVENTS) <= 12
    assert "TASK_COMPLETED" not in enums.ALERTABLE_EVENTS
    assert "AGENT_HEARTBEAT" not in enums.ALERTABLE_EVENTS
