"""Goals -> work, by deterministic rules (§30, §36A). No model call decides whether work exists.

The event router turns failures into incidents; nothing turned the company's own goals
into work. Every watchdog tick this module looks at each ACTIVE goal and opens at most
one task per rule. A goal without a metric selector is not measurable here and is skipped.

    GOAL_ACHIEVED    verified reading at or past the
                     target (either direction)        -> founder confirms and closes it
                     (no planning or "behind" for a goal that is already met)
    GOAL_CONFIG_INVALID  baseline/target not numeric or equal, or a malformed
                     selector (checked first, whatever the metric reads) -> founder repairs the
                     goal; no other rule fires for it
    GOAL_UNMEASURED  selector set, newest reading
                     missing or STALE                -> developer restores the measurement
                     (the other rules wait for it: planning on no number is guessing)
    GOAL_NO_WORK     no open or parked task for it    -> CEO plans the next step
    GOAL_BEHIND      progress below the straight line
                     from creation to deadline        -> CEO re-plans

An anomaly wakes an agent; an agent does not look for anomalies. Tasks are attended
(`autonomous: False`) unless the founder sets RECONCILER_AUTONOMOUS_PLANNING=true: then the
two planning rules are opted in to the unattended worker. Planning has no side effect (no
shell, repository, deploy, publish or spend); the worker still applies its LOW-risk gate,
the CEO's daily run budget and the supervisor, and the result lands in REVIEW with its
proposals as BACKLOG tasks (app/task_materializer.py). GOAL_UNMEASURED stays attended. Dedupe is the open-task unique index on `dedupe_key`;
a task closed within COOLDOWN is not reopened, so finishing a plan is not answered by
the next tick with the same plan.
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, enums, goal_evidence, goals, keys, metrics_store, models

log = logging.getLogger("company.business_reconciler")

# ponytail: straight-line trajectory and a fixed slack; per-goal curves if growth is seasonal.
BEHIND_SLACK = 0.10
COOLDOWN = timedelta(days=7)
CLOSED = (enums.TaskStatus.DONE, enums.TaskStatus.FAILED, enums.TaskStatus.CANCELLED)
HAS_WORK = [*enums.OPEN_TASK_STATES, enums.TaskStatus.BACKLOG]
PLANNING = ("GOAL_NO_WORK", "GOAL_BEHIND")  # never opted in: GOAL_CONFIG_INVALID is a founder repair


def autonomous_planning() -> bool:
    return os.environ.get("RECONCILER_AUTONOMOUS_PLANNING", "false").lower() == "true"


@dataclass(frozen=True)
class Rule:
    agent: str | None
    title: str
    objective: str
    success_criteria: str
    founder: bool = False   # a founder decision: approval_required, never autonomous


RULES = {
    "GOAL_ACHIEVED": Rule(
        None, "Goal {id} reached its target: confirm and close",
        "Goal \"{title}\" reads {value} against a target of {target} ({metric} from {source}, verified "
        "{observed_at}). Confirm the reading and close the goal as ACHIEVED, or keep it open "
        "with a reason.",
        "Goal status set by the founder: ACHIEVED, or kept ACTIVE with the reason recorded.",
        founder=True),
    "GOAL_CONFIG_INVALID": Rule(
        None, "Goal {id} cannot be evaluated: fix its configuration",
        "Goal \"{title}\" cannot be evaluated because its own configuration is invalid ({issues}). "
        "Correct the goal's baseline, target or metric selector, or close the goal. The goal was not "
        "changed automatically, and no planning is done on it until it is valid.",
        "Goal configuration corrected by the founder, or the goal closed or abandoned.",
        founder=True),
    "GOAL_UNMEASURED": Rule(
        "developer", "Goal {id} has no current measurement",
        "Restore the metric feed for goal {id} ({metric}): find why {selector} has no reading "
        "within its freshness window ({stale_window}) and fix the reporter, without inventing a value. "
        "Why it is unmeasured: {reason}.",
        "A VERIFIED observation for the goal's selector; cause recorded."),
    "GOAL_NO_WORK": Rule(
        "ceo", "Goal {id} has no work: plan the next step",
        "Goal \"{title}\" is ACTIVE but nothing is open for it. Propose the single highest-value "
        "next task toward {target} ({metric}), from recorded state.",
        "One task linked to the goal with an objective, owner and success criteria, or a "
        "recorded reason why none is needed."),
    "GOAL_BEHIND": Rule(
        "ceo", "Goal {id} behind trajectory: re-plan",
        "Goal \"{title}\" is at {progress:.0%} of the way to {target}; a straight line to "
        "{deadline} expects {expected:.0%}. Find why and propose what changes.",
        "Cause recorded with evidence; plan adjusted, or the goal's target/deadline change "
        "proposed to the founder."),
}


# What would have to be true for a different decision; fixed text, so the snapshot is deterministic.
WOULD_CHANGE = {
    "GOAL_ACHIEVED": "progress below 1 on a verified reading, or a founder decision to keep the goal open",
    "GOAL_CONFIG_INVALID": "a numeric baseline, a numeric target different from it, and a well-formed metric selector",
    "GOAL_UNMEASURED": "a VERIFIED, unambiguous, finite observation inside the source's freshness window",
    "GOAL_NO_WORK": "an open, backlog or parked task linked to the goal",
    "GOAL_BEHIND": "progress within BEHIND_SLACK of the straight line from creation to deadline",
}


def _snapshot(kind: str, goal: models.Goal, ev: goal_evidence.GoalEvidence, why: str,
              expected: float | None = None, config: list[str] | None = None) -> dict:
    """Bounded, JSON-safe record of why a rule fired. Persisted facts only; no payloads or reasoning."""
    sel = ev.metric_selector if isinstance(ev.metric_selector, dict) else {}
    trusted = ev.measurement_status == "VERIFIED"
    missing = [] if trusted else [ev.reason_code]
    if config:
        missing = config + missing   # the goal's own faults first; the reading is judged separately
    return {"rule": kind, "why": why, "goal_id": goal.id, "goal_title": (goal.title or "")[:120],
            "selector": {k: v[:200] for k in ("subject", "metric", "window", "source")
                         if isinstance(v := sel.get(k), str)},
            "measurement_status": ev.measurement_status, "reason_code": config[0] if config else ev.reason_code,
            **({"config_issues": config, "measurement_reason_code": ev.reason_code} if config else {}),
            "observation_id": ev.observation_id, "observed_at": ev.observed_at,
            "source": ev.source if trusted else None,
            "value": ev.latest_value if trusted else None, "unit": ev.measurement_unit if trusted else None,
            "baseline": ev.baseline, "target": ev.target, "progress": ev.progress, "expected_progress": expected,
            "missing_evidence": missing, "would_change_if": WOULD_CHANGE[kind],
            "evidence_timestamp": ev.evidence_timestamp}


def findings(db: Session, goal: models.Goal, now: datetime) -> list[tuple[str, dict]]:
    """Which rules this goal triggers, with the facts each task will cite."""
    if goal.metric_selector is None:
        # No selector (absent, not malformed: {} or "" fall through to GOAL_CONFIG_INVALID): Company OS cannot measure this goal (e.g. a backup or a baseline goal),
        # so "restore the feed" would be a task nobody can finish. The founder tracks it.
        return []
    ev = goal_evidence.for_goal(db, goal, now)
    facts = {"id": goal.id, "title": goal.title, "metric": goal.metric, "target": goal.target,
             "selector": goal.metric_selector,
             "stale_window": metrics_store.stale_after(goals.selector_valid(goal.metric_selector) and goal.metric_selector.get("source")),
             "deadline": goal.deadline.date().isoformat() if goal.deadline else None}
    issues = goals.config_issues(goal)
    if issues:
        # Fail closed: no achievement, planning or trajectory on a goal that cannot be evaluated,
        # and the reporter is not blamed for the goal's own fault.
        why = f"goal configuration is invalid ({', '.join(issues)}); the reading is {ev.measurement_status}"
        return [("GOAL_CONFIG_INVALID", facts | {"issues": ", ".join(issues),
                                                 "evidence": _snapshot("GOAL_CONFIG_INVALID", goal, ev, why, config=issues)})]
    if ev.measurement_status != "VERIFIED":
        why = f"measurement is {ev.measurement_status}: {ev.explanation}"
        return [("GOAL_UNMEASURED", facts | {"reason": ev.explanation,
                                             "evidence": _snapshot("GOAL_UNMEASURED", goal, ev, why)})]
    if ev.progress is not None and ev.progress >= 1:
        # Met is met: do not re-plan or call it behind. Closing it stays the founder's call.
        why = f"verified reading {ev.latest_value} is at or past the target {goal.target}"
        return [("GOAL_ACHIEVED", facts | {"value": ev.latest_value, "source": ev.source,
                                           "observed_at": ev.observed_at,
                                           "evidence": _snapshot("GOAL_ACHIEVED", goal, ev, why)})]
    out = []
    has_work = db.execute(select(models.Task.id).where(
        models.Task.goal_id == goal.id, models.Task.status.in_(HAS_WORK)).limit(1)).first()
    if not has_work:
        out.append(("GOAL_NO_WORK", facts | {"evidence": _snapshot(
            "GOAL_NO_WORK", goal, ev, "goal is ACTIVE, measured, and has no open, backlog or parked task")}))
    if ev.progress is not None and goal.deadline:
        start, end = metrics_store._aware(goal.created_at), metrics_store._aware(goal.deadline)
        if end > start:
            expected = min(1.0, max(0.0, (now - start) / (end - start)))
            if ev.progress < expected - BEHIND_SLACK:
                why = f"progress {ev.progress:.0%} is below the {expected:.0%} straight line by more than {BEHIND_SLACK:.0%}"
                out.append(("GOAL_BEHIND", facts | {"progress": ev.progress, "expected": expected,
                                                    "evidence": _snapshot("GOAL_BEHIND", goal, ev, why, expected)}))
    return out


def _cooling_down(db: Session, dedupe: str, now: datetime) -> bool:
    last = db.execute(select(models.Task.updated_at).where(
        models.Task.dedupe_key == dedupe, models.Task.status.in_(CLOSED))
        .order_by(models.Task.updated_at.desc()).limit(1)).scalar_one_or_none()
    return last is not None and now - metrics_store._aware(last) < COOLDOWN


def _open(db: Session, goal: models.Goal, kind: str, facts: dict, now: datetime) -> str | None:
    dedupe = f"goal:{goal.id}:{kind}"
    if _cooling_down(db, dedupe, now):
        return None
    rule = RULES[kind]
    agent = db.execute(select(models.Agent).where(models.Agent.key == rule.agent)).scalar_one_or_none() \
        if rule.agent else None
    project = db.get(models.Project, goal.project_id) if goal.project_id else None
    task = models.Task(
        title=rule.title.format(**facts)[:300],
        description=f"Opened automatically by the business reconciler ({kind}).",
        objective=rule.objective.format(**facts), success_criteria=rule.success_criteria,
        status=enums.TaskStatus.READY, priority=goal.priority, risk_level=enums.RiskLevel.LOW,
        approval_required=rule.founder,
        assigned_agent_id=agent.id if agent else None, goal_id=goal.id,
        project_id=goal.project_id, business_unit_id=project.business_unit_id if project else None,
        created_by="business-reconciler", dedupe_key=dedupe,
        # scope: a goal without a project still passes the worker's quality gate.
        context={"goal": goal.id, "rule": kind, "scope": f"goal:{goal.id}", "evidence": facts["evidence"],
                 "autonomous": kind in PLANNING and autonomous_planning() and not rule.founder},
    )
    try:
        keys.add_with_key(db, task, "TASK")
    except keys.DuplicateOpenWork:  # already open: this tick has nothing to add
        db.rollback()
        return None
    audit.record(db, actor="business-reconciler", action="task.create", entity_type="task",
                 entity_key=task.key, reason=f"{kind} on goal {goal.id}", task_id=task.id)
    db.commit()
    return task.key


class Reconciled(list):
    """The keys of the tasks opened this run (still a plain list to every caller), plus per-goal evidence:
    how many ACTIVE goals were examined and which ones failed. "Examined without error" is all it says; it is
    never a claim that a goal is on track or achieved."""
    def __init__(self, keys=(), goals_total: int = 0, failed_goal_ids: list[int] | None = None):
        super().__init__(keys)
        self.goals_total = goals_total
        self.failed_goal_ids = failed_goal_ids or []


def reconcile(db: Session, now: datetime | None = None) -> Reconciled:
    """Keys of the tasks opened this run. One goal failing never stops the others; it is recorded, not hidden."""
    now = now or datetime.now(timezone.utc)
    opened = Reconciled()
    for goal in db.execute(select(models.Goal).where(
            models.Goal.status == enums.GoalStatus.ACTIVE).order_by(models.Goal.id)).scalars().all():
        opened.goals_total += 1
        try:
            for kind, facts in findings(db, goal, now):
                key = _open(db, goal, kind, facts, now)
                if key:
                    opened.append(key)
        except Exception:
            db.rollback()
            opened.failed_goal_ids.append(goal.id)
            log.exception("business reconcile failed for goal %s", goal.id)
    return opened
