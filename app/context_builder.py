"""Build the minimum useful context packet for one assigned task (§36A, §118)."""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, models
from app.registry import AgentSpec


def _iso(value):
    return value.isoformat() if value else None


def _scope(model, task: models.Task):
    """Prefer the exact project; fall back to its business unit."""
    if task.project_id is not None:
        return model.project_id == task.project_id
    if task.business_unit_id is not None:
        return model.business_unit_id == task.business_unit_id
    return False


# The facts that opened the task (the business reconciler's goal measurement: value, baseline, target, source,
# observed_at). Bounded so a malformed producer cannot flood the window.
MAX_TRIGGER_EVIDENCE_CHARS = 4000


def _trigger_evidence(raw_context: dict) -> dict | None:
    evidence = raw_context.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return None
    if len(json.dumps(evidence, default=str)) > MAX_TRIGGER_EVIDENCE_CHARS:
        return {"omitted": f"trigger evidence exceeds {MAX_TRIGGER_EVIDENCE_CHARS} characters"}
    return evidence


def build(db: Session, task: models.Task, spec: AgentSpec) -> dict:
    project = db.get(models.Project, task.project_id) if task.project_id else None
    unit = db.get(models.BusinessUnit, task.business_unit_id) if task.business_unit_id else None

    decisions = list(db.execute(
        select(models.Decision).where(
            _scope(models.Decision, task),
            models.Decision.status == enums.DecisionStatus.ACTIVE,
        ).order_by(models.Decision.created_at.desc()).limit(10)
    ).scalars()) if task.project_id or task.business_unit_id else []

    learnings = list(db.execute(
        select(models.Learning).where(_scope(models.Learning, task),
                                      models.Learning.status == enums.LearningStatus.APPROVED)
        .order_by(models.Learning.created_at.desc()).limit(10)
    ).scalars()) if task.project_id or task.business_unit_id else []

    note_filters = [
        models.AgentNote.agent_id == task.assigned_agent_id,
        models.AgentNote.archived_at.is_(None),
    ]
    if task.project_id is not None:
        note_filters.append(models.AgentNote.project_id == task.project_id)
    else:
        note_filters.append(models.AgentNote.task_id == task.id)
    notes = list(db.execute(
        select(models.AgentNote).where(*note_filters)
        .order_by(models.AgentNote.last_seen_at.desc()).limit(10)
    ).scalars())

    run_stmt = (
        select(models.AgentRun, models.Task.key)
        .join(models.Task, models.AgentRun.task_id == models.Task.id)
        .where(models.AgentRun.agent_id == task.assigned_agent_id)
        .order_by(models.AgentRun.started_at.desc())
        .limit(3)
    )
    if task.project_id is not None:
        run_stmt = run_stmt.where(models.Task.project_id == task.project_id)
    else:
        run_stmt = run_stmt.where(models.AgentRun.task_id == task.id)
    runs = list(db.execute(run_stmt))

    raw_context = task.context if isinstance(task.context, dict) else {}
    raw_files = raw_context.get("required_files") or raw_context.get("files") or []
    if not isinstance(raw_files, list):
        raw_files = []
    files = [item.strip() for item in raw_files if isinstance(item, str) and item.strip()]
    max_files = max(0, int(spec.context_policy.get("max_files", 25)))
    required_files = files[:max_files]

    tool_state = raw_context.get("required_tool_state") or []
    if not isinstance(tool_state, list):
        tool_state = []

    return {
        "task": {
            "key": task.key, "title": task.title, "description": task.description,
            "objective": task.objective, "success_criteria": task.success_criteria,
            "status": task.status.value, "priority": task.priority.value,
            "risk_level": task.risk_level.value,
            "approval_required": task.approval_required,
        },
        "agent": {"key": spec.key, "role": spec.role,
                  "model_class": spec.model_class, "prompt_version": spec.prompt_version},
        "business_unit": ({"key": unit.key, "name": unit.name,
                           "status": unit.status.value, "priority": unit.priority.value}
                          if unit else None),
        "project": ({"key": project.key, "name": project.name,
                     "status": project.status.value, "priority": project.priority.value,
                     "objective": project.objective, "current_phase": project.current_phase,
                     "repository": project.repository}
                    if project else None),
        "decisions": [
            {"key": row.key, "title": row.title, "decision": row.decision,
             "reason": row.reason}
            for row in decisions
        ],
        "learnings": [
            {"title": row.title, "lesson": row.lesson, "evidence": row.evidence,
             "tags": row.tags}
            for row in learnings
        ],
        "agent_notes": [
            {"note": row.note, "tags": row.tags, "references": row.reference_count,
             "last_seen_at": _iso(row.last_seen_at)}
            for row in notes
        ],
        "recent_runs": [
            {"task": task_key, "status": row.status.value, "result": row.result,
             "error": row.error,
             "supervisor_verdict": (row.supervisor_verdict.value
                                    if row.supervisor_verdict else None),
             "started_at": _iso(row.started_at), "ended_at": _iso(row.ended_at)}
            for row, task_key in runs
        ],
        "required_files": required_files,
        "truncated_files": len(files) - len(required_files),
        "required_tool_state": tool_state,
        # Without this a planning task saw only its title and earlier runs, and repeatedly concluded the goal's
        # metric was "not recorded" although the reconciler had measured it (TASK-0078/0079/0084, 2026-10-24).
        "trigger_evidence": _trigger_evidence(raw_context),
        "context_policy": spec.context_policy,
    }
