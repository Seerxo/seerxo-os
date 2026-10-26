"""Task -> herdr workflow mapping.

The risk-to-reviewer mapping is the part worth protecting: it is what makes a HIGH task
get two independent reviewers without a human remembering to ask.
"""
import pytest

from app import dispatch, enums, models


def _task(**kw) -> models.Task:
    t = models.Task(
        key=kw.pop("key", "TASK-0001"), title=kw.pop("title", "Do the thing"),
        success_criteria=kw.pop("success_criteria", "It is demonstrably done"),
    )
    t.status = kw.pop("status", enums.TaskStatus.READY)
    t.risk_level = kw.pop("risk_level", enums.RiskLevel.LOW)
    t.priority = kw.pop("priority", enums.Priority.P2)
    t.approval_required = kw.pop("approval_required", False)
    t.retry_count = kw.pop("retry_count", 0)
    t.max_attempts = kw.pop("max_attempts", 3)
    t.context = kw.pop("context", None)
    t.project_id = None
    return t


def _project(repository: str | None = None, key: str = "proj") -> models.Project:
    p = models.Project(key=key, name=key)
    p.repository = repository
    return p


# --- risk -> workflow ------------------------------------------------------------

@pytest.mark.parametrize("risk,workflow", [
    (enums.RiskLevel.LOW, "quick"),
    (enums.RiskLevel.MEDIUM, "standard"),
    (enums.RiskLevel.HIGH, "high-risk"),
    (enums.RiskLevel.CRITICAL, "high-risk"),
])
def test_risk_selects_workflow(risk, workflow):
    assert dispatch.plan_for(_task(risk_level=risk), None).workflow == workflow


def test_every_risk_level_is_mapped():
    # A new risk level must not silently fall through to the cheapest workflow.
    assert set(dispatch.WORKFLOW_BY_RISK) == set(enums.RiskLevel)


# --- the approval gate -----------------------------------------------------------

def test_approval_required_suppresses_auto():
    plan = dispatch.plan_for(_task(approval_required=True), None)
    assert plan.auto is False
    assert plan.auto_blocked_by == "task.approval_required"


def test_high_risk_suppresses_auto_even_without_approval_flag():
    plan = dispatch.plan_for(_task(risk_level=enums.RiskLevel.HIGH), None)
    assert plan.auto is False
    assert "risk_level=HIGH" in plan.auto_blocked_by


def test_low_risk_unflagged_task_may_run_automatically():
    assert dispatch.plan_for(_task(), None).auto is True


def test_high_risk_plan_says_why_it_is_expensive():
    plan = dispatch.plan_for(_task(risk_level=enums.RiskLevel.CRITICAL), None)
    assert any("two independent reviewers" in n for n in plan.notes)


# --- dispatchability -------------------------------------------------------------

@pytest.mark.parametrize("status", sorted(dispatch.NON_DISPATCHABLE, key=lambda s: s.value))
def test_non_dispatchable_states_are_refused(status):
    with pytest.raises(ValueError, match=status.value):
        dispatch.plan_for(_task(status=status), None)


@pytest.mark.parametrize("status", [
    enums.TaskStatus.READY, enums.TaskStatus.QUEUED,
    enums.TaskStatus.IN_PROGRESS, enums.TaskStatus.REVIEW,
])
def test_workable_states_are_allowed(status):
    assert dispatch.plan_for(_task(status=status), None).task == "TASK-0001"


def test_waiting_external_is_refused_because_nobody_can_act_on_it():
    # TASK-0001 waits on R2 credentials; dispatching it would start work that cannot finish.
    with pytest.raises(ValueError):
        dispatch.plan_for(_task(status=enums.TaskStatus.WAITING_EXTERNAL), None)


# --- traceability and repository handling ----------------------------------------

def test_hctl_id_is_the_company_os_key_verbatim():
    # Keeps the worktree name traceable and lets the reporter match runs back with no
    # lookup table.
    assert dispatch.plan_for(_task(key="TASK-0042"), None).hctl_id == "TASK-0042"


def test_repository_caveat_is_surfaced_not_dropped():
    proj = _project("semihbugrasezer/youtube-auto (server copy is NOT a git repo)")
    plan = dispatch.plan_for(_task(), proj)
    assert plan.repository == "semihbugrasezer/youtube-auto"
    assert any("NOT a git repo" in n for n in plan.notes)


def test_plain_repository_is_left_alone():
    plan = dispatch.plan_for(_task(), _project("seerxo/os"))
    assert plan.repository == "seerxo/os"
    assert plan.notes == []


def test_retry_count_is_surfaced():
    plan = dispatch.plan_for(_task(retry_count=2), None)
    assert any("2/3" in n for n in plan.notes)


def test_scope_comes_from_task_context():
    plan = dispatch.plan_for(_task(context={"scope": "src/auth"}), None)
    assert plan.scope == "src/auth"


# --- the command -----------------------------------------------------------------

def test_command_carries_acceptance_and_workflow():
    argv = dispatch.shell_command(dispatch.plan_for(_task(), None), "/repo")
    assert argv[:5] == ["hctl", "task", "start", "TASK-0001", "/repo"]
    assert "--acceptance=It is demonstrably done" in argv
    assert "--workflow=quick" in argv
    assert "--auto" in argv


def test_command_requires_the_worker_to_load_the_minimal_context_packet():
    plan = dispatch.plan_for(_task(key="TASK-0042"), None, agent_key="developer")
    assert plan.context_command == \
        "ssh company-os-host sudo companyctl context TASK-0042"
    argv = dispatch.shell_command(plan, "/repo")
    assert "--acceptance=Load task context: ssh company-os-host sudo companyctl context " \
           "TASK-0042" in argv


def test_command_omits_auto_when_approval_is_required():
    plan = dispatch.plan_for(_task(approval_required=True), None)
    assert "--auto" not in dispatch.shell_command(plan, "/repo")


def test_command_includes_scope_only_when_present():
    with_scope = dispatch.shell_command(
        dispatch.plan_for(_task(context={"scope": "src/x"}), None), "/repo")
    assert "--scope=src/x" in with_scope
    assert not any(a.startswith("--scope") for a in
                   dispatch.shell_command(dispatch.plan_for(_task(), None), "/repo"))


# --- §45 enforcement is tested at the schema level here; the endpoint check lives in
#     main.update_task_status and is exercised against the live API.

def test_workflow_map_never_downgrades_critical():
    # A regression that mapped CRITICAL to a cheaper workflow would silently remove the
    # second reviewer from the most dangerous tasks.
    assert dispatch.WORKFLOW_BY_RISK[enums.RiskLevel.CRITICAL] == \
        dispatch.WORKFLOW_BY_RISK[enums.RiskLevel.HIGH] == "high-risk"


def test_approval_required_beats_low_risk():
    # The cheap workflow is still chosen, but automation is off: cost and gating are
    # independent decisions.
    plan = dispatch.plan_for(_task(risk_level=enums.RiskLevel.LOW, approval_required=True), None)
    assert plan.workflow == "quick"
    assert plan.auto is False


# --- outer-loop eligibility ------------------------------------------------------

def _agent(key="developer", enabled=True):
    agent = models.Agent(key=key, role=key, enabled=enabled)
    agent.id = 1
    return agent


def _business_unit(status=enums.BusinessUnitStatus.ACTIVE):
    unit = models.BusinessUnit(key="seerxo", name="Seerxo", status=status)
    unit.id = 1
    return unit


def test_outer_loop_skips_unassigned_work():
    assert dispatch.skip_reason(_task(), None, None, []) == "unassigned"


def test_outer_loop_skips_disabled_agent():
    assert dispatch.skip_reason(_task(), _agent(enabled=False), None, []) == \
        "agent_disabled"


def test_outer_loop_skips_inactive_business_unit():
    reason = dispatch.skip_reason(
        _task(), _agent(), _business_unit(enums.BusinessUnitStatus.BACKLOG), [])
    assert reason == "business_unit_not_active"


def test_outer_loop_skips_unfinished_dependency():
    reason = dispatch.skip_reason(
        _task(), _agent(), _business_unit(), [enums.TaskStatus.IN_PROGRESS])
    assert reason == "dependencies_incomplete"


def test_outer_loop_accepts_enabled_owner_with_finished_dependencies():
    reason = dispatch.skip_reason(
        _task(), _agent(), _business_unit(), [enums.TaskStatus.DONE])
    assert reason is None


def test_dispatch_plan_records_the_selected_agent():
    plan = dispatch.plan_for(_task(), None, agent_key="research")
    assert plan.agent == "research"
    assert plan.workflow == "research"
