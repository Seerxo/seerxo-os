"""Agent-proposed work -> real tasks, by deterministic policy (§6, §36A, §70).

An approved run's `new_task_candidates` used to stay inside agent_runs.tool_calls, so a
CEO that found the next step reported it and nothing happened. They now become tasks,
under rules a model cannot widen:

- only from an APPROVED run (the supervisor accepted the result first);
- at most MAX_PER_RUN per run, extra candidates dropped and counted;
- depth-bounded: a task proposed by an agent cannot propose further tasks (MAX_DEPTH = 1), so agent -> 3
  tasks -> 9 -> 27 cannot happen. The one exception is a terminal hand-off: an auto-READY research task
  (depth 1) may propose code_change tasks for the developer (depth 2, HANDOFF_MAX_DEPTH). Nothing else is
  accepted at depth 1 and nothing at all at depth 2, so CEO -> Research -> Developer is the longest chain;
- the parent's goal, project and business unit are inherited, never chosen by the model;
- routed by app/task_policy.py: read-only research/analysis becomes READY for the research
  agent (when PROPOSAL_AUTO_READY is on), side-effect wording becomes BACKLOG with
  approval_required, destructive wording is refused, the rest is BACKLOG and unassigned.
  Every created task counts as work for its goal, so the reconciler does not re-plan;
- one candidate per (parent, title): a retried run does not duplicate its proposals.
"""
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, control, enums, keys, models, task_policy

HANDOFF_AGENT = "developer"
FILE_LIKE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w-]+\.(?:py|ya?ml|sh|md|json|toml|txt|cfg|ini|sql|env|html|tsx?|jsx?|css|lock)\b")
MAX_PER_RUN = 3
MAX_DEPTH = 1
HANDOFF_MAX_DEPTH = 2   # reachable only by a developer hand-off from an auto-READY research task
ACTOR = "task-materializer"


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]


def _handoff_paths(title: str, reason: str) -> list[str]:
    """Allow-listed files a code_change proposal literally names. The model proposes, this list decides:
    a file outside SAFE_FILES (protected code, config, workflows, anything unknown) is never scoped."""
    from app import stage_c_worker  # late: stage_c_worker imports the worker, which imports this module
    text = f"{title} {reason}"
    # Any other file-looking name (protected code, config, workflows, unknown paths) means the intent is wider than
    # the allow-list, so nothing is scoped: the proposal waits for a person instead of being silently narrowed.
    named = set(FILE_LIKE.findall(text))
    if named - stage_c_worker.SAFE_FILES:
        return []
    return sorted(named & stage_c_worker.SAFE_FILES)


def _admissible(db: Session, parent: models.Task, paths: list[str]) -> bool:
    """Would Stage C admission accept this hand-off today (ACTIVE project, approved repository, allow-listed
    scope)? A missing project or unsupported repository must wait in BACKLOG, not be marked autonomous."""
    probe = models.Task(risk_level=enums.RiskLevel.LOW, approval_required=False, project_id=parent.project_id,
                        context={"autonomous": True, "write": True, "stage_c": {"paths": paths}})
    from app import stage_c_worker  # late: see _handoff_paths
    return stage_c_worker.admission(db, probe) is None


def materialize(db: Session, parent: models.Task, run: models.AgentRun, candidates: list) -> dict:
    """Create tasks for an approved run's candidates, routed by task_policy. Flushes; the
    caller commits."""
    pctx = parent.context if isinstance(parent.context, dict) else {}
    depth = int(pctx.get("depth") or 0) + 1
    out = {"created": [], "auto_ready": [], "approval": [], "refused": [], "dropped": 0, "reason": None}
    if not candidates:
        return out
    if (parent.id is None or parent.assigned_agent_id is None or run.id is None
            or run.task_id != parent.id or run.agent_id != parent.assigned_agent_id
            or run.status != enums.AgentRunStatus.SUCCEEDED
            or run.supervisor_verdict != enums.SupervisorVerdict.APPROVED):
        return {**out, "dropped": len(candidates), "reason": "run_not_approved_for_parent"}
    owner = db.get(models.Agent, parent.assigned_agent_id) if parent.assigned_agent_id else None
    terminal = (depth == HANDOFF_MAX_DEPTH and pctx.get("policy") == "AUTO" and pctx.get("autonomous") is True
                and owner is not None and owner.key == task_policy.AUTO_AGENT)
    if owner is not None and owner.key == HANDOFF_AGENT:  # the developer's own output never creates tasks
        return {**out, "dropped": len(candidates), "reason": f"{parent.key} is the developer's task (no children)"}
    if depth > MAX_DEPTH and not terminal:
        return {**out, "dropped": len(candidates),
                "reason": f"{parent.key} was itself proposed by an agent (depth limit {MAX_DEPTH})"}
    if terminal:  # only code_change hand-offs pass; research/planning children would be fan-out
        kept = [c for c in candidates if getattr(c, "kind", "other") == "code_change"]
        candidates, out["dropped"] = kept, len(candidates) - len(kept)
        if not kept:
            return {**out, "reason": f"{parent.key} was itself proposed by an agent (depth limit {MAX_DEPTH}; only a "
                                     "code_change hand-off may go one level deeper)"}
    out["dropped"] += max(0, len(candidates) - MAX_PER_RUN)
    research = db.execute(select(models.Agent).where(
        models.Agent.key == task_policy.AUTO_AGENT)).scalar_one_or_none()
    developer = db.execute(select(models.Agent).where(
        models.Agent.key == HANDOFF_AGENT)).scalar_one_or_none()
    sha = os.environ.get("COMPANY_OS_GIT_SHA", "")
    scope = pctx.get("scope") or (f"goal:{parent.goal_id}" if parent.goal_id else None)
    for c in candidates[:MAX_PER_RUN]:
        title = " ".join((c.title or "").split())[:300]
        if len(title) < 3:
            continue
        reason = " ".join((c.reason or "").split())[:2000]
        kind = getattr(c, "kind", "other")
        decision = task_policy.classify(title, reason, kind)
        if decision.route == "FORBIDDEN":
            out["refused"].append({"title": title, "why": decision.reason})
            continue
        dedupe = f"candidate:{parent.key}:{_slug(title)}"[:200]
        if db.execute(select(models.Task.id).where(models.Task.dedupe_key == dedupe)).first():
            continue
        auto = decision.route == "AUTO" and research is not None
        # Research -> Developer: a code_change proposal becomes the Developer's task, carrying its origin. It is
        # READY only when the founder's standing authorization already covers every file it names; otherwise it
        # waits in BACKLOG as a scoped proposal. Either way Stage C admission, host acceptance and the code-write
        # flag decide whether anything is ever written.
        handoff = decision.route == "BACKLOG" and kind == "code_change" and developer is not None
        paths = _handoff_paths(title, reason) if handoff else []
        covered = bool(paths) and control.standing_covers(db, paths, sha) and _admissible(db, parent, paths)
        task = models.Task(
            title=title, description=f"Proposed by {parent.key} (run {run.id}).",
            objective=reason or title,
            success_criteria=("A written answer to the objective with cited evidence; no action taken."
                              if auto else
                              "Exactly the named files change, the full test suite passes with recorded output and an independent provider approves."
                              if paths else "To be agreed before this task is made READY."),
            status=enums.TaskStatus.READY if auto or covered else enums.TaskStatus.BACKLOG,
            priority=parent.priority, risk_level=enums.RiskLevel.LOW,
            approval_required=decision.route == "APPROVAL",
            assigned_agent_id=research.id if auto else developer.id if handoff else None,
            goal_id=parent.goal_id, project_id=parent.project_id,
            business_unit_id=parent.business_unit_id, created_by=ACTOR, dedupe_key=dedupe,
            context={"parent": parent.key, "origin_run": run.id, "depth": depth, "reason": reason,
                     "kind": kind, "policy": "AUTO" if auto else "HANDOFF" if handoff else decision.route,
                     "policy_reason": decision.reason, "autonomous": auto or covered,
                     **({"scope": scope} if scope else {}),
                     **({"handoff": {"from": parent.key, "run": run.id}} if handoff else {}),
                     **({"write": True, "stage_c": {"paths": paths}} if paths else {})},
        )
        keys.add_with_key(db, task, "TASK")
        audit.record(db, actor=ACTOR, action="task.create", entity_type="task", entity_key=task.key,
                     reason=f"candidate from {parent.key} run {run.id}: {decision.route}", task_id=task.id)
        out["created"].append(task.key)
        if auto:
            out["auto_ready"].append(task.key)
        if handoff:
            out.setdefault("handoff", []).append({"task": task.key, "paths": paths, "ready": covered})
        if decision.route == "APPROVAL":
            out["approval"].append(task.key)
    return out
