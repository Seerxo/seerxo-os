"""Hand a Company OS task down to herdr's control center (docs/AGENT_EXECUTION.md §4.1).

Company OS decides *why* a task matters and *what done means*. herdr decides *how* the
work gets done. This module is the translation between the two vocabularies, and nothing
more — it never executes anything.

The mapping rules live here rather than in the shell script so they are testable and so
there is one place to change them. `hctl` itself runs on the laptop; the server only
produces the plan.
"""
from dataclasses import asdict, dataclass

from app import enums, models

# §34 risk level selects reviewer strength. This is the point of the whole mapping:
# a HIGH task gets two independent reviewers (router.toml `high-risk`: Sol writer plus
# Codex *and* Claude review) without anyone having to remember to ask for them.
WORKFLOW_BY_RISK = {
    enums.RiskLevel.LOW: "quick",        # fast writer, no reviewer
    enums.RiskLevel.MEDIUM: "standard",  # writer + one reviewer
    enums.RiskLevel.HIGH: "high-risk",   # complex writer + reviewer + architect
    enums.RiskLevel.CRITICAL: "high-risk",
}

# Tasks in these states are not ready to be executed by anyone.
NON_DISPATCHABLE = {
    enums.TaskStatus.IDEA, enums.TaskStatus.BACKLOG, enums.TaskStatus.DONE,
    enums.TaskStatus.CANCELLED, enums.TaskStatus.WAITING_APPROVAL,
    enums.TaskStatus.WAITING_EXTERNAL, enums.TaskStatus.BLOCKED,
}


@dataclass
class DispatchPlan:
    task: str
    agent: str | None
    hctl_id: str
    title: str
    acceptance: str
    workflow: str
    risk_level: str
    priority: str
    auto: bool
    auto_blocked_by: str | None
    repository: str | None
    project: str | None
    scope: str | None
    context_command: str
    notes: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def plan_for(task: models.Task, project: models.Project | None,
             agent_key: str | None = None) -> DispatchPlan:
    """Pure mapping. Raises ValueError when the task must not be dispatched."""
    if task.status in NON_DISPATCHABLE:
        raise ValueError(
            f"{task.key} is {task.status.value} — dispatch only READY, QUEUED, "
            f"IN_PROGRESS or REVIEW tasks"
        )

    notes: list[str] = []
    workflow = "research" if agent_key == "research" else WORKFLOW_BY_RISK[task.risk_level]

    # §72/§112: an approval-gated task must stop for the founder. hctl's --auto still
    # halts at fingerprint approval, but not passing it keeps the gate visible rather
    # than relying on a flag's fine print.
    auto = True
    blocked_by = None
    if task.approval_required:
        auto, blocked_by = False, "task.approval_required"
    elif task.risk_level in (enums.RiskLevel.HIGH, enums.RiskLevel.CRITICAL):
        auto, blocked_by = False, f"risk_level={task.risk_level.value}"

    if task.risk_level in (enums.RiskLevel.HIGH, enums.RiskLevel.CRITICAL):
        notes.append("high-risk workflow: two independent reviewers (codex + claude)")
    if task.retry_count:
        notes.append(f"previously attempted {task.retry_count}/{task.max_attempts} times")

    repository = project.repository if project else None
    if repository and "(" in repository:
        # The registry sometimes carries a parenthetical caveat, e.g.
        # "semihbugrasezer/youtube-auto (server copy is NOT a git repo)". Keep the bare
        # name for path resolution and surface the caveat instead of silently dropping it.
        bare, _, caveat = repository.partition("(")
        notes.append("repository note: " + caveat.rstrip(") ").strip())
        repository = bare.strip()

    scope = (task.context or {}).get("scope") if task.context else None

    return DispatchPlan(
        task=task.key,
        agent=agent_key,
        # The Company OS key is used verbatim as the hctl task id, so the worktree is
        # task/TASK-0006-<slug> and the reporter can match runs back without a lookup table.
        hctl_id=task.key,
        title=task.title,
        acceptance=task.success_criteria,
        workflow=workflow,
        risk_level=task.risk_level.value,
        priority=task.priority.value,
        auto=auto,
        auto_blocked_by=blocked_by,
        repository=repository,
        project=project.key if project else None,
        scope=scope,
        context_command=f"ssh company-os-host sudo companyctl context {task.key}",
        notes=notes,
    )


def skip_reason(task: models.Task, agent: models.Agent | None,
                business_unit: models.BusinessUnit | None,
                dependency_statuses: list[enums.TaskStatus], *, require_ready: bool = True,
                allowed_statuses: tuple | None = None) -> str | None:
    """Deterministic outer-loop gate. No model is called to decide if work exists."""
    allowed = ((enums.TaskStatus.READY, enums.TaskStatus.QUEUED) if require_ready
               else (enums.TaskStatus.WAITING_EXTERNAL,))
    if task.status not in (allowed_statuses if allowed_statuses is not None else allowed):
        return "task_not_ready"
    if agent is None:
        return "unassigned"
    if not agent.enabled:
        return "agent_disabled"
    if business_unit is not None and business_unit.status != enums.BusinessUnitStatus.ACTIVE:
        return "business_unit_not_active"
    if any(status != enums.TaskStatus.DONE for status in dependency_statuses):
        return "dependencies_incomplete"
    return None


def shell_command(plan: DispatchPlan, repo_path: str) -> list[str]:
    """The argv `hctl` should be invoked with, on the laptop."""
    argv = ["hctl", "task", "start", plan.hctl_id, repo_path, plan.title,
            f"--acceptance={plan.acceptance}",
            f"--acceptance=Load task context: {plan.context_command}",
            f"--workflow={plan.workflow}"]
    if plan.scope:
        argv.append(f"--scope={plan.scope}")
    if plan.auto:
        argv.append("--auto")
    return argv
