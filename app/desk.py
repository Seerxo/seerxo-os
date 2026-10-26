"""PostgreSQL read models for the Founder Desk."""
import functools
import os
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from pathlib import Path

from app import business_reconciler, enums, goals, metrics_store, models, reconciler_heartbeat, watchdog, worker
from app.notify import NotificationService

COST_POLICY = Path(__file__).resolve().parent.parent / "agents" / "cost_policy.yaml"
ROOT = Path(__file__).resolve().parent.parent
API_STARTED_AT = datetime.now(timezone.utc).isoformat()


def cost_view(db: Session) -> dict:
    """Costs as classes, not a single "$0": the company has approved costs; the metric
    that must stay zero is new spend an agent created."""
    policy = yaml.safe_load(COST_POLICY.read_text()) if COST_POLICY.exists() else {"items": {}}
    runs = db.execute(select(models.AgentRun.tool_calls)).scalars().all()
    new_spend = sum(float((tc or {}).get("incremental_cost_usd") or 0) for tc in runs)
    return {"new_autonomous_spend_usd": new_spend,
            "items": [{"key": k, **v} for k, v in (policy.get("items") or {}).items()]}
from app.registry import AgentSpec


def _iso(value):
    # Stored instants are UTC; a naive value (SQLite) must not be read as browser-local time.
    if not value:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _run(row: models.AgentRun, task_key: str | None = None) -> dict:
    return {
        "id": row.id, "task": task_key, "status": row.status.value,
        "provider": row.provider, "model_class": row.model,
        "started_at": _iso(row.started_at), "ended_at": _iso(row.ended_at),
        "cost_usd": float(row.estimated_cost_usd or 0), "result": row.result,
        "error": row.error,
        "supervisor_verdict": row.supervisor_verdict.value if row.supervisor_verdict else None,
        "supervisor_notes": row.supervisor_notes,
    }


def origin(task: models.Task) -> dict:
    """Why a task exists, from what its creator recorded (no new state): who opened it and
    the trigger in its context. Detail views add the audit reason and the goal."""
    ctx = task.context if isinstance(task.context, dict) else {}
    by = task.created_by or "unknown"
    if ctx.get("rule") and ctx.get("goal"):
        trigger = f"{ctx['rule']} on goal {ctx['goal']}"
    elif ctx.get("incident"):
        trigger = f"incident {ctx['incident']}"
    elif ctx.get("parent"):
        trigger = f"proposed by {ctx['parent']} (run {ctx.get('origin_run')})"
    elif ctx.get("guardian"):
        trigger = "verified CI failure on main"
    elif ctx.get("source") == "founder-desk-command":
        trigger = "Founder Desk request"
    else:
        trigger = None
    return {"by": by, "automatic": by != "founder", "trigger": trigger}


def _task_summary(task: models.Task, agents: dict[int, models.Agent],
                  projects: dict[int, models.Project], units: dict[int, models.BusinessUnit]) -> dict:
    agent = agents.get(task.assigned_agent_id)
    project = projects.get(task.project_id)
    unit = units.get(task.business_unit_id)
    return {
        "key": task.key, "title": task.title, "status": task.status.value,
        "priority": task.priority.value, "risk": task.risk_level.value,
        "agent": agent.key if agent else None,
        "project": project.key if project else None,
        "business_unit": unit.key if unit else None,
        "approval_required": task.approval_required, "origin": origin(task),
        "created_at": _iso(task.created_at), "updated_at": _iso(task.updated_at),
    }


def _agent_status(agent: models.Agent, tasks: list[models.Task],
                  runs: list[models.AgentRun], paused: bool) -> tuple[str, models.Task | None]:
    current = next((t for t in tasks if t.status == enums.TaskStatus.IN_PROGRESS), None)
    if current is None:
        current = next((t for t in tasks if t.status in (
            enums.TaskStatus.WAITING_APPROVAL, enums.TaskStatus.BLOCKED,
            enums.TaskStatus.WAITING_EXTERNAL, enums.TaskStatus.REVIEW,
            enums.TaskStatus.QUEUED, enums.TaskStatus.READY,
        )), None)
    if paused or not agent.enabled:
        return "PAUSED", current
    latest = runs[0] if runs else None
    if (latest and latest.status == enums.AgentRunStatus.STARTED and not latest.ended_at) or (
        current and current.status == enums.TaskStatus.IN_PROGRESS
    ):
        # "Working" needs recent evidence: a run or task untouched past the worker's own
        # stale-run limit is stuck, not working (the Desk showed 4 working with a dead worker).
        touched = max([_aware(x) for x in (latest.started_at if latest else None,
                                           current.updated_at if current else None) if x],
                      default=None)
        if touched is None or datetime.now(timezone.utc) - touched > worker.STALE_AFTER:
            return "STALLED", current
        return "RUNNING", current
    if current and current.status == enums.TaskStatus.WAITING_APPROVAL:
        return "NEEDS_APPROVAL", current
    if current and current.status in (enums.TaskStatus.BLOCKED, enums.TaskStatus.WAITING_EXTERNAL):
        return "BLOCKED", current
    if current and current.status == enums.TaskStatus.REVIEW:
        return "WAITING", current
    # A failed provider attempt is history; FAILED needs a still-failed task.
    if latest and latest.status == enums.AgentRunStatus.FAILED and any(
        t.id == latest.task_id and t.status == enums.TaskStatus.FAILED for t in tasks
    ):
        return "FAILED", current
    return "SLEEPING", current


def _agent_summary(agent: models.Agent, spec: AgentSpec, tasks: list[models.Task],
                   runs: list[models.AgentRun], paused: bool) -> dict:
    status, current = _agent_status(agent, tasks, runs, paused)
    today = datetime.now(timezone.utc).date()
    cost = sum(float(run.estimated_cost_usd or 0) for run in runs
               if run.started_at and run.started_at.date() == today)
    last_success = next((run.started_at for run in runs
                         if run.status == enums.AgentRunStatus.SUCCEEDED), None)
    return {
        "key": agent.key, "name": spec.name, "role": spec.role,
        "team": spec.team, "reports_to": spec.reports_to,
        "business_units": spec.business_units,
        "enabled": agent.enabled, "status": status,
        "current_task": ({"key": current.key, "title": current.title,
                          "status": current.status.value} if current else None),
        "last_run": _iso(runs[0].started_at) if runs else None,
        "last_success": _iso(agent.last_success_at or last_success),
        "last_error": agent.last_error or next((r.error for r in runs if r.error), None),
        "cost_today_usd": round(cost, 4), "model_class": spec.model_class,
    }


def run_series(runs: list[models.AgentRun], days: int = 14) -> list[dict]:
    """Daily run outcomes for the Desk trend chart, oldest first, gaps as zero rows."""
    # ponytail: built from build_state's 200-run window; a busier company needs a SQL GROUP BY.
    today = datetime.now(timezone.utc).date()
    series = {today.fromordinal(today.toordinal() - i).isoformat():
              {s.value: 0 for s in enums.AgentRunStatus} for i in range(days)}
    for run in runs:
        bucket = series.get(run.started_at.date().isoformat()) if run.started_at else None
        if bucket is not None:
            bucket[run.status.value] += 1
    return [{"day": day, **counts} for day, counts in sorted(series.items())]


def task_lead_time(tasks: list[models.Task], now: datetime | None = None) -> dict:
    """Created-to-DONE hours for tasks closed in the last 30 days; no sample stays unknown."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    samples = []
    for task in tasks:
        if task.status != enums.TaskStatus.DONE or not task.completed_at:
            continue
        created = task.created_at if task.created_at.tzinfo else task.created_at.replace(tzinfo=timezone.utc)
        completed = task.completed_at if task.completed_at.tzinfo else task.completed_at.replace(tzinfo=timezone.utc)
        if cutoff <= completed <= now and created <= completed:
            samples.append((completed - created).total_seconds() / 3600)
    return {"median_hours": round(median(samples), 1) if samples else None,
            "sample_size": len(samples),
            "review_pending": sum(t.status == enums.TaskStatus.REVIEW for t in tasks)}


# Founder attention queue: deterministic, ordered by the authority the founder alone holds.
# Lower rank first. No model decides what needs the founder.
_RANK = {"SEV0": 0, "SECURITY": 1, "SEV1": 2, "STOPPED": 3, "APPROVAL": 4,
         "BLOCKED_P0": 5, "BLOCKED_P1": 6, "PROVIDER": 7, "OTHER": 8}
_BLOCKED = (enums.TaskStatus.BLOCKED, enums.TaskStatus.WAITING_EXTERNAL, enums.TaskStatus.FAILED)


def provider_health(w: dict, now: datetime) -> dict[str, str]:
    """Effective provider health: an active cooldown overrides the last local check."""
    out = dict(w.get("providers") or {})
    for name, c in (w.get("cooldown") or {}).items():
        if c.get("until") and datetime.fromisoformat(c["until"]) > now:
            out[name] = c.get("health") or out.get(name)
    return out


def needs_you(approvals, incidents, open_tasks, w: dict, backup, paused: bool,
              now: datetime | None = None, affected: dict | None = None,
              reconciler: dict | None = None, notifications=(),
              recovery_events=(), backup_verified: bool = False) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    items = []

    def add(kind, level, title, why, affected, since, evidence, action, page, key=None):
        items.append({"kind": kind, "rank": _RANK[kind], "level": level, "title": title,
                      "why": why, "affected": affected, "since": _iso(since) if isinstance(since, datetime) else since,
                      "evidence": evidence, "action": action, "target": {"page": page, "key": key}})

    for i in incidents:
        if i.severity in (enums.IncidentSeverity.SEV0, enums.IncidentSeverity.SEV1):
            # What is affected (unit/service), not who detected it.
            add(i.severity.value, "critical", i.title, "A production problem is open.",
                (affected or {}).get(i.key) or i.detected_by, i.started_at, f"{i.key} · {i.status.value}",
                "Open the incident and confirm an owner is acting", "incidents", i.key)
    for n in notifications:
        if n.delivery_status == "route_failed":
            add("STOPPED", "critical", "Watchdog could not open incident work",
                "Event routing failed after three bounded attempts; the source condition remains active.",
                "watchdog", n.attempted_at, f"event {n.event_id} · route_failed",
                "Repair a missing Service mapping; the next observation permits one bounded redrive. Other route failures require founder review.",
                "activity")
        else:
            add("SECURITY", "critical", "Critical alert delivery is unconfirmed",
                "Telegram may have accepted this alert; automatic retries are stopped to avoid duplicates.",
                "notification", n.attempted_at, f"event {n.event_id} · {n.delivery_status}",
                "Inspect the event and retry with verified founder identity if needed", "activity")
    for event in recovery_events:
        pending = event.notify_reason == "recovery delivery pending"
        add("SECURITY", "critical", "Recovery alert delivery needs review",
            ("The process stopped after recording send intent; Telegram may have received the recovery alert."
             if pending else
             "The original alert may have reached Telegram; recovery remains blocked until a founder reviews it."
             if event.notify_reason == "recovery blocked by uncertain alert delivery" else
             "No recovery send began; inspect the source alert and event, then retry as founder if safe."
             if event.notify_reason == "recovery dispatch retries exhausted; founder review required" else
             "Telegram recovery delivery failed or is ambiguous; automatic retries are stopped."),
            "watchdog", event.occurred_at, f"event {event.id} · {event.notify_reason}",
            "Inspect the event and verify Telegram before taking any retry action", "activity")
    health = provider_health(w, now)
    if w.get("last") == "zero_cost_guard" or "METERED_BLOCKED" in health.values():
        add("SECURITY", "critical", "Metered credentials found; worker refused to run",
            "Per-token billing would create new spend (§26A).", "company-worker",
            w.get("heartbeat_at"), "zero-cost guard", "Remove the metered key; no task ran", "health")
    if w.get("status") == "STALE":
        add("STOPPED", "critical", "Autonomous worker stopped reporting",
            "No unattended work happens while the worker is silent.", "company-worker",
            w.get("heartbeat_at"), f"last heartbeat {w.get('heartbeat_age_s')}s ago",
            "Check the company-worker container", "health")
    if backup is None or backup.target != "company-db":
        add("STOPPED", "critical", "No verified off-site database backup",
            "Without an integrity-verified off-site copy, a host failure may lose company memory.", "company-postgres",
            None, "never recorded", "Run and verify the backup", "health")
    elif not (backup_verified and backup.ok and backup.integrity_verified and backup.uploaded):
        add("STOPPED", "critical", "Latest database backup is not verified off-site",
            "The latest company-db run has no matching BACKUP_VERIFIED evidence.", "company-postgres",
            backup.started_at,
            f"ok={backup.ok} uploaded={backup.uploaded} integrity={backup.integrity_verified} "
            f"verified_event={backup_verified}",
            "Run and verify the backup", "health")
    elif (now - _aware(backup.started_at)).total_seconds() > watchdog.BACKUP_MAX_AGE_H * 3600:
        add("STOPPED", "warning", "Latest verified backup is too old",
            f"Daily backups are expected; older than {watchdog.BACKUP_MAX_AGE_H}h means runs were missed.",
            "company-postgres", backup.started_at, f"target {backup.target}",
            "Check the backup cron", "health")
    if reconciler is not None:
        r = reconciler
        if r["outcome"] not in ("COMPLETED", "STARTED") or r["freshness"] != "VERIFIED":
            # Missing, stale, raised, unfinished or untrusted evidence: goals are not being checked.
            add("STOPPED", "warning", "Business reconciler is not verifiably running",
                "Goal checks only happen when the watchdog completes a reconcile run.", "watchdog",
                r["last_attempt_at"], f"{r['outcome']} · {r['freshness']}"
                + (f" · {r['error']}" if r["error"] else ""), "Check the host watchdog schedule and its log", "health")
        elif r["per_goal_outcome"] == "FAILED":
            add("OTHER", "warning", f"Business reconciler failed for {r['goal_errors']} goal(s)",
                "The run completed, but those goals were skipped this tick.", "watchdog",
                r["last_attempt_at"], f"{r['goal_errors']} goal error(s) logged",
                "Read the watchdog log for 'business reconcile failed'", "health")
    for a in approvals:
        add("APPROVAL", "warning", a.action, a.reason, a.requested_by, a.created_at,
            f"{a.key} · risk {a.risk_level.value}", "Approve or reject", "approvals", a.key)
    for t in open_tasks:
        if t.approval_required and t.status == enums.TaskStatus.READY:
            # e.g. the reconciler's "goal reached: confirm and close": only the founder acts.
            add("APPROVAL", "info", t.title, t.objective or "A founder decision is required.",
                t.key, t.created_at, f"{t.key} · opened by {t.created_by}", "Decide on the task",
                "tasks", t.key)
        if t.status in _BLOCKED and t.priority in (enums.Priority.P0, enums.Priority.P1):
            add(f"BLOCKED_{t.priority.value}", "warning", t.title, "High-priority work cannot progress.",
                t.key, t.updated_at, f"{t.key} · {t.status.value}", "Open the task and unblock it",
                "tasks", t.key)
    auth = sorted(n for n, h in health.items() if h in ("AUTH_REQUIRED", "BROKEN"))
    if auth:
        add("PROVIDER", "warning", f"{', '.join(auth).title()} needs attention",
            "The provider cannot run until it is logged in or repaired.", ", ".join(auth),
            w.get("heartbeat_at"), ", ".join(f"{n}: {health[n]}" for n in auth),
            "Log in again with the founder subscription", "health")
    elif health and all(h in ("QUOTA_EXHAUSTED", "RATE_LIMITED") for h in health.values()):
        add("PROVIDER", "info", "No model capacity until the subscription resets",
            "Tasks wait safely; no retry is consumed. Nothing to buy.", ", ".join(sorted(health)),
            w.get("heartbeat_at"), ", ".join(f"{n}: {h}" for n, h in sorted(health.items())),
            "Wait for the reset", "health")
    if paused:
        add("OTHER", "info", "Agents are paused", "Autonomous execution is stopped by founder control.",
            "all agents", None, "control: agents_paused", "Resume when ready", "overview")
    return sorted(items, key=lambda x: (x["rank"], x["since"] or ""))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)



def current_priority(goals, open_tasks, agents_by_id, progress_of=None) -> dict | None:
    """What the company is focused on, from persisted state only (no generated text).
    An ACTIVE goal wins; otherwise the highest-priority open P0/P1 task, labelled as such.
    `progress_of(goal)` supplies measured progress (app.goals.progress); without it, none."""
    goal = next((g for g in sorted(goals, key=lambda g: g.priority.value)
                 if g.status == enums.GoalStatus.ACTIVE), None)
    if goal:
        serving = [t for t in open_tasks if t.goal_id == goal.id]
        return {"kind": "goal", "id": goal.id, "title": goal.title, "why": goal.description,
                "metric": goal.metric, "baseline": goal.baseline, "target": goal.target,
                "deadline": _iso(goal.deadline), "priority": goal.priority.value,
                "open_tasks": len(serving), "top_task": serving[0].key if serving else None,
                "measure": progress_of(goal) if progress_of else None}
    task = next((t for t in open_tasks if t.priority in (enums.Priority.P0, enums.Priority.P1)), None)
    if task:
        agent = agents_by_id.get(task.assigned_agent_id)
        return {"kind": "task", "key": task.key, "title": task.title, "why": task.objective,
                "success": task.success_criteria, "owner": agent.key if agent else None,
                "status": task.status.value, "priority": task.priority.value}
    return None


IMPROVEMENT_DONE = {"MEASURED", "ABANDONED"}


def improvements(tasks: list) -> list[dict]:
    """Stage C work in flight (stage_c lifecycle on task.context). Later evidence wins."""
    out = []
    for t in tasks:
        imp = (t.context or {}).get("improvement") or {}
        if not imp.get("stage") or imp["stage"] in IMPROVEMENT_DONE:
            continue
        ev = {}
        for step in imp.get("history", []):
            ev.update(step.get("evidence") or {})
        out.append({"task": t.key, "title": t.title, "stage": imp["stage"],
                    "updated_at": imp["history"][-1]["at"] if imp.get("history") else None,
                    **{k: ev.get(k) for k in ("repository", "branch", "pr_number", "pr_state",
                                              "ci_state", "failed_check", "repairs",
                                              "review_verdict", "risk", "expected_effect",
                                              "verified_at")}})
    return sorted(out, key=lambda i: i["updated_at"] or "", reverse=True)


@functools.lru_cache(maxsize=1)
def _code_migration_head() -> str | None:
    """The newest migration shipped in this image (alembic heads; CI enforces a single head)."""
    try:
        cfg = Config()
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        return None


def _guardian_enabled() -> bool | None:
    try:
        value = yaml.safe_load((ROOT / "agents" / "repository_guardian.yaml").read_text()).get("enabled")
        return value if isinstance(value, bool) else None
    except Exception:
        return None


def runtime_view(db: Session, worker_view: dict) -> dict:
    """What is live, next to what it should be (§9, §165). `drift` lists every mismatch;
    unknown values are reported as unknown, never as a match."""
    api_sha = os.environ.get("COMPANY_OS_GIT_SHA") or None
    worker_rt = worker_view.get("runtime") or {}
    db_rev = (db.execute(text("select version_num from alembic_version")).scalar()
              if inspect(db.connection()).has_table("alembic_version") else None)
    head = _code_migration_head()
    autonomy = worker_view.get("autonomy") or {}
    drift = []
    if not api_sha or not worker_rt.get("git_sha"):
        drift.append("source SHA unknown (deploy sets COMPANY_OS_GIT_SHA)")
    elif api_sha != worker_rt["git_sha"]:
        drift.append(f"api runs {api_sha[:7]}, worker runs {worker_rt['git_sha'][:7]}")
    if not db_rev or not head or db_rev != head:
        drift.append(f"database migration {db_rev} is not this code's head {head}")
    return {"api": {"git_sha": api_sha, "started_at": API_STARTED_AT},
            "worker": {"git_sha": worker_rt.get("git_sha"), "image": worker_rt.get("image"),
                       "started_at": worker_rt.get("started_at")},
            "migration": {"database": db_rev, "code_head": head},
            "stage_c_enabled": autonomy.get("code_write_flag"),
            "reconciler_autonomous_planning": business_reconciler.autonomous_planning(),
            "guardian_enabled": _guardian_enabled(), "drift": drift}


def build_state(db: Session, specs: dict[str, AgentSpec], paused: bool) -> dict:
    agents = list(db.execute(select(models.Agent).where(
        models.Agent.key.in_(specs)
    )).scalars())
    tasks = list(db.execute(select(models.Task).order_by(
        models.Task.priority, models.Task.created_at.desc()
    )).scalars())
    projects = list(db.execute(select(models.Project)).scalars())
    units = list(db.execute(select(models.BusinessUnit)).scalars())
    runs = list(db.execute(select(models.AgentRun).order_by(
        models.AgentRun.started_at.desc(), models.AgentRun.id.desc()
    ).limit(200)).scalars())
    incidents = list(db.execute(select(models.Incident).where(
        models.Incident.status != enums.IncidentStatus.RESOLVED
    ).order_by(models.Incident.severity, models.Incident.started_at)).scalars())
    delivered_incidents = NotificationService.delivered_incident_ids(db, incidents)
    approvals = list(db.execute(select(models.Approval).where(
        models.Approval.status == enums.ApprovalStatus.REQUESTED
    ).order_by(models.Approval.created_at)).scalars())
    services = list(db.execute(select(models.Service).order_by(models.Service.key)).scalars())
    events = list(db.execute(select(models.Event).order_by(
        models.Event.occurred_at.desc(), models.Event.id.desc()
    ).limit(40)).scalars())
    condition_scopes = {
        (event.payload or {}).get("condition_scope")
        for event in events
        if event.source == "watchdog" and isinstance((event.payload or {}).get("condition_scope"), str)
    }
    observation_keys = [watchdog._condition_checkpoint_key(scope) for scope in condition_scopes]
    condition_observations = {
        row.key: row.value or {}
        for row in db.scalars(select(models.ControlState).where(
            models.ControlState.key.in_(observation_keys)
        )).all()
    } if observation_keys else {}
    notification_attention = list(db.scalars(select(models.NotificationState).where(
        models.NotificationState.active.is_(True),
        models.NotificationState.delivery_status.in_(("pending", "uncertain", "route_failed"))).limit(20)))
    recovery_attention = list(db.scalars(select(models.Event).where(
        models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED",
        models.Event.notify_reason.in_(("recovery delivery pending",
                                        "recovery delivery failed or uncertain",
                                        "recovery blocked by uncertain alert delivery",
                                        "recovery dispatch retries exhausted; founder review required"))
    ).order_by(models.Event.occurred_at.desc()).limit(20)))
    active_watchdog_conditions = []
    active_condition_query = select(models.NotificationState).where(
        models.NotificationState.scope_key.startswith(watchdog.WATCHDOG_CONDITION_PREFIX),
        models.NotificationState.active.is_(True),
    )
    active_watchdog_conditions_total = db.scalar(select(func.count()).select_from(
        models.NotificationState).where(
            models.NotificationState.scope_key.startswith(watchdog.WATCHDOG_CONDITION_PREFIX),
            models.NotificationState.active.is_(True),
        )) or 0
    active_condition_states = db.scalars(active_condition_query.order_by(
        models.NotificationState.scope_key).limit(100)).all()
    for condition_state in active_condition_states:
        source_event = db.get(models.Event, condition_state.event_id) if condition_state.event_id else None
        if source_event is None:
            continue
        source_payload = source_event.payload or {}
        checkpoint = db.get(models.ControlState,
                            watchdog._condition_checkpoint_key(condition_state.scope_key))
        latest = (checkpoint.value or {}).get("latest_observation") if checkpoint else None
        active_watchdog_conditions.append({
            "event_id": source_event.id,
            "type": source_event.event_type,
            "severity": (latest or {}).get("severity", source_event.severity.value),
            "title": (latest or {}).get("title") or source_payload.get("title") or source_event.event_type,
            "detail": (latest or {}).get("detail") or source_payload.get("detail"),
            "observed_at": (latest or {}).get("observed_at") or source_payload.get("last_seen_at"),
            "episode": condition_state.episode,
            "delivery_status": condition_state.delivery_status,
        })
    host = db.get(models.ControlState, watchdog.HOST_METRICS_KEY)
    company_backup = db.execute(select(models.BackupRun).where(
        models.BackupRun.target == "company-db", models.BackupRun.restore_tested_at.is_(None)
    ).order_by(models.BackupRun.started_at.desc(), models.BackupRun.id.desc()).limit(1)).scalar_one_or_none()
    verified_company_backup = watchdog._last_verified_backup(
        db, restore=False, target="company-db")
    company_backup_verified = bool(
        company_backup is not None and verified_company_backup is not None
        and company_backup.id == verified_company_backup.id
    )
    restore = db.execute(select(models.BackupRun).where(
        models.BackupRun.target == "company-db", models.BackupRun.restore_tested_at.is_not(None)
    ).order_by(models.BackupRun.restore_tested_at.desc(), models.BackupRun.id.desc()).limit(1)).scalar_one_or_none()

    agent_by_id = {a.id: a for a in agents}
    project_by_id = {p.id: p for p in projects}
    unit_by_id = {b.id: b for b in units}
    task_by_id = {t.id: t for t in tasks}
    service_by_id = {s.id: s for s in services}

    def unit_of(unit_id=None, service_id=None, project_id=None, task=None):
        """Business unit key behind an incident/event, via its unit, service, project or task."""
        uid = (unit_id or getattr(service_by_id.get(service_id), "business_unit_id", None)
               or getattr(project_by_id.get(project_id), "business_unit_id", None)
               or (task.business_unit_id if task else None))
        return unit_by_id[uid].key if uid in unit_by_id else None
    open_tasks = [t for t in tasks if t.status in enums.OPEN_TASK_STATES]

    agent_rows = []
    for agent in agents:
        own_tasks = [t for t in tasks if t.assigned_agent_id == agent.id]
        own_runs = [r for r in runs if r.agent_id == agent.id]
        agent_rows.append(_agent_summary(agent, specs[agent.key], own_tasks, own_runs, paused))
    agent_rows.sort(key=lambda a: (a["reports_to"] != "founder", a["name"]))

    degraded = any(i.severity in (enums.IncidentSeverity.SEV0, enums.IncidentSeverity.SEV1)
                   for i in incidents) or any(
        s.status in (enums.ServiceStatus.DEGRADED, enums.ServiceStatus.FAILING)
        for s in services
    )
    priorities = [t for t in open_tasks if t.priority in (enums.Priority.P0, enums.Priority.P1)]

    worker_view = worker.worker_state(db)
    # "Alive" (heartbeat) is not "useful": the last worker run that actually succeeded.
    useful = [r.ended_at for r in runs if r.provider in worker.WORKER_PROVIDERS
              and r.status == enums.AgentRunStatus.SUCCEEDED and r.ended_at]
    worker_view["last_success_at"] = _iso(max(useful)) if useful else None
    revenue = metrics_store.revenue(db)
    reconciler_execution = reconciler_heartbeat.view(db)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "control": {"agents_paused": paused},
        "worker": worker_view,
        "runtime": runtime_view(db, worker_view),
        "reconciler_execution": reconciler_execution,
        "needs_you": needs_you(approvals, incidents, open_tasks, worker_view, company_backup, paused,
                               reconciler=reconciler_execution,
                               notifications=notification_attention,
                               recovery_events=recovery_attention,
                               backup_verified=company_backup_verified,
                               affected={i.key: " · ".join(x for x in (
                                   (unit_by_id.get(service_by_id[i.service_id].business_unit_id).name
                                    if i.service_id in service_by_id
                                    and service_by_id[i.service_id].business_unit_id in unit_by_id else None),
                                   service_by_id[i.service_id].name if i.service_id in service_by_id else None) if x)
                                   for i in incidents}),
        "portfolio": metrics_store.portfolio(db),
        "improvements": improvements(tasks),
        "current_priority": current_priority(
            list(db.execute(select(models.Goal)).scalars()), open_tasks, agent_by_id,
            progress_of=lambda g: goals.progress(db, g)),
        "costs": cost_view(db),
        "run_series": run_series(runs),
        "task_lead_time": task_lead_time(tasks),
        "revenue": revenue,
        # What a new task can be attached to (Create Task form). Closed goals take no new work.
        "catalog": {
            "projects": [{"key": p.key, "name": p.name,
                          "business_unit": unit_by_id[p.business_unit_id].key
                          if p.business_unit_id in unit_by_id else None} for p in projects],
            "goals": [{"id": g.id, "title": g.title, "status": g.status.value,
                       "project": project_by_id[g.project_id].key if g.project_id in project_by_id else None}
                      for g in db.execute(select(models.Goal).order_by(models.Goal.id)).scalars()
                      if g.status not in goals.CLOSED],
        },  # with source, observed_at and freshness; None = no verified source
        "overview": {
            # Null until a revenue source reports. Null is evidence; zero would be invented data.
            "revenue_today": revenue and revenue["today"], "revenue_month": revenue and revenue["month"],
            "active_projects": sum(p.status == enums.ProjectStatus.ACTIVE for p in projects),
            "running_agents": sum(a["status"] == "RUNNING" for a in agent_rows),
            "open_tasks": len(open_tasks),
            "blocked_tasks": sum(t.status in (enums.TaskStatus.BLOCKED,
                                               enums.TaskStatus.WAITING_EXTERNAL)
                                 for t in open_tasks),
            "incidents": len(incidents), "pending_approvals": len(approvals),
            "system_health": "DEGRADED" if degraded else "HEALTHY",
        },
        "priorities": [_task_summary(t, agent_by_id, project_by_id, unit_by_id)
                       for t in priorities[:3]],
        "agents": agent_rows,
        "tasks": [_task_summary(t, agent_by_id, project_by_id, unit_by_id) for t in tasks],
        "approvals": [
            {"key": a.key, "action": a.action, "reason": a.reason,
             "expected_effect": a.expected_effect, "risk": a.risk_level.value,
             "rollback": a.rollback, "requested_by": a.requested_by,
             "created_at": _iso(a.created_at)} for a in approvals
        ],
        "incidents": [
            {"key": i.key, "title": i.title, "description": i.description,
             "business_unit": unit_of(service_id=i.service_id, project_id=i.project_id),
             "service": service_by_id[i.service_id].name if i.service_id in service_by_id else None,
             "severity": i.severity.value, "status": i.status.value,
             "detected_by": i.detected_by, "started_at": _iso(i.started_at),
             "root_cause": i.root_cause, "founder_notified": i.id in delivered_incidents}
            for i in incidents
        ],
        "health": {
            "api": "ACTIVE", "postgresql": "ACTIVE",
            # Latest watchdog reading (every 10 min); "at" lets the Desk show its age.
            "resources": {k: (host.value if host else {}).get(k)
                          for k in ("cpu_percent", "ram_percent", "disk_percent", "at")},
            "collection": ((host.value if host else {}).get("collection") or {
                "containers": "UNKNOWN", "container_health": "UNKNOWN"}),
            "watchdog_conditions": active_watchdog_conditions,
            "watchdog_conditions_total": active_watchdog_conditions_total,
            "watchdog_conditions_truncated": active_watchdog_conditions_total > len(active_watchdog_conditions),
            "services": [
                {"key": s.key, "name": s.name, "status": s.status.value,
                 "risk": s.risk.value, "last_verified": _iso(s.last_verified)}
                for s in services
            ],
            "backup": ({"target": company_backup.target, "ok": company_backup.ok,
                        "uploaded": company_backup.uploaded,
                        "integrity_verified": company_backup.integrity_verified,
                        "verified": company_backup_verified,
                        "at": _iso(company_backup.started_at)} if company_backup else None),
            "last_restore": ({"target": restore.target, "ok": restore.ok,
                              "uploaded": restore.uploaded,
                              "integrity_verified": restore.integrity_verified,
                              "at": _iso(restore.restore_tested_at)} if restore else None),
        },
        "activity": [
            {"id": e.id, "type": e.event_type, "source": e.source,
             "severity": e.severity.value, "at": _iso(e.occurred_at),
             "notified": e.notified, "notify_reason": e.notify_reason,
             "title": (e.payload or {}).get("title") or e.event_type,
             "detail": (e.payload or {}).get("detail") or (e.payload or {}).get("message"),
             "latest_observation": _latest_watchdog_observation(e, condition_observations),
             "task": task_by_id[e.task_id].key if e.task_id in task_by_id else None,
             "business_unit": unit_of(unit_id=e.business_unit_id, service_id=e.service_id,
                                      task=task_by_id.get(e.task_id))}
            for e in events
        ],
    }


def _latest_watchdog_observation(event: models.Event, checkpoints: dict) -> dict | None:
    payload = event.payload or {}
    scope = payload.get("condition_scope")
    if event.source != "watchdog" or not isinstance(scope, str):
        return None
    checkpoint = checkpoints.get(watchdog._condition_checkpoint_key(scope)) or {}
    if checkpoint.get("event_id") != event.id:
        return None
    observation = checkpoint.get("latest_observation")
    return observation if isinstance(observation, dict) else None


def agent_detail(db: Session, agent: models.Agent, spec: AgentSpec, paused: bool) -> dict:
    tasks = list(db.execute(select(models.Task).where(
        models.Task.assigned_agent_id == agent.id
    ).order_by(models.Task.id.desc()).limit(20)).scalars())
    run_rows = list(db.execute(
        select(models.AgentRun, models.Task.key)
        .outerjoin(models.Task, models.AgentRun.task_id == models.Task.id)
        .where(models.AgentRun.agent_id == agent.id)
        .order_by(models.AgentRun.started_at.desc(), models.AgentRun.id.desc()).limit(20)
    ))
    runs = [row for row, _ in run_rows]
    notes = list(db.execute(select(models.AgentNote).where(
        models.AgentNote.agent_id == agent.id, models.AgentNote.archived_at.is_(None)
    ).order_by(models.AgentNote.last_seen_at.desc()).limit(20)).scalars())
    project_ids = {t.project_id for t in tasks if t.project_id is not None}
    learnings = list(db.execute(select(models.Learning).where(
        models.Learning.project_id.in_(project_ids)
    ).order_by(models.Learning.created_at.desc()).limit(10)).scalars()) if project_ids else []
    summary = _agent_summary(agent, spec, tasks, runs, paused)
    unattended = []
    waiting = {enums.TaskStatus.QUEUED: "awaiting_ready",
               enums.TaskStatus.WAITING_EXTERNAL: "waiting_external",
               enums.TaskStatus.WAITING_APPROVAL: "approval_required",
               enums.TaskStatus.REVIEW: "awaiting_review",
               enums.TaskStatus.BLOCKED: "blocked",
               enums.TaskStatus.IN_PROGRESS: "running"}
    for task in tasks:
        if task.status not in (*waiting, enums.TaskStatus.READY, enums.TaskStatus.QUEUED):
            continue
        if paused:
            reason = "agents_paused"
        elif not agent.enabled:
            reason = "agent_disabled"
        elif task.status in waiting:
            reason = waiting[task.status]
        else:
            # Reuse execution eligibility: the Desk must not invent a second scheduler.
            reason, _ = worker._eligible(db, task, {spec.key: spec})
        unattended.append({"task": task.key, "status": task.status.value,
                           "reason": reason, "approval_required": task.approval_required})
    return {
        **summary,
        "unattended_tasks": unattended,
        "identity": {
            "description": spec.description, "capabilities": spec.capabilities,
            "owns": spec.owns, "skills": spec.skills,
            "allowed_tools": spec.allowed_tools, "forbidden_tools": spec.forbidden_tools,
            "business_units": spec.business_units, "projects": spec.projects,
            "prompt_version": spec.prompt_version,
        },
        "schedule": spec.schedule, "triggers": spec.triggers,
        "memory_policy": spec.memory_policy, "context_policy": spec.context_policy,
        "recent_tasks": [{"key": t.key, "title": t.title, "status": t.status.value,
                          "priority": t.priority.value} for t in tasks],
        "recent_runs": [_run(row, key) for row, key in run_rows],
        "recent_learnings": [{"title": x.title, "lesson": x.lesson, "status": x.status.value,
                              "created_at": _iso(x.created_at)} for x in learnings],
        "notebook": [{"note": n.note, "refs": n.reference_count,
                      "last_seen_at": _iso(n.last_seen_at)} for n in notes],
        "errors": [r.error for r in runs if r.error][:10],
    }


def task_detail(db: Session, task: models.Task) -> dict:
    agent = db.get(models.Agent, task.assigned_agent_id) if task.assigned_agent_id else None
    project = db.get(models.Project, task.project_id) if task.project_id else None
    unit = db.get(models.BusinessUnit, task.business_unit_id) if task.business_unit_id else None
    dependencies = list(db.execute(
        select(models.Task).join(
            models.TaskDependency, models.Task.id == models.TaskDependency.depends_on_id
        ).where(models.TaskDependency.task_id == task.id).order_by(models.Task.key)
    ).scalars())
    run_rows = list(db.execute(select(models.AgentRun).where(
        models.AgentRun.task_id == task.id
    ).order_by(models.AgentRun.started_at.desc(), models.AgentRun.id.desc()).limit(20)).scalars())
    verdict = next((r for r in run_rows if r.supervisor_verdict), None)
    created = db.execute(select(models.AuditLog.reason).where(
        models.AuditLog.task_id == task.id,
        models.AuditLog.action.in_(("task.create", "incident.create")))
        .order_by(models.AuditLog.id).limit(1)).scalar_one_or_none()
    goal = db.get(models.Goal, task.goal_id) if task.goal_id else None
    return {
        "key": task.key, "title": task.title, "description": task.description,
        "objective": task.objective, "status": task.status.value,
        "priority": task.priority.value, "risk": task.risk_level.value,
        "assigned_agent": agent.key if agent else None,
        "project": project.key if project else None,
        "business_unit": unit.key if unit else None,
        "success_criteria": task.success_criteria, "context": task.context,
        "origin": {**origin(task), "reason": created},
        "goal": {"id": goal.id, "title": goal.title, "status": goal.status.value} if goal else None,
        "approval_required": task.approval_required,
        # Attempts used and allowed: the worker stops retrying at the limit (the Desk says so before Mark READY).
        "retry_count": task.retry_count, "max_attempts": task.max_attempts,
        "dependencies": [{"key": d.key, "status": d.status.value} for d in dependencies],
        "runs": [_run(r, task.key) for r in run_rows],
        "artifacts": task.artifacts, "result": task.result, "error": task.error,
        "supervisor": ({"verdict": verdict.supervisor_verdict.value,
                        "notes": verdict.supervisor_notes} if verdict else None),
        "created_at": _iso(task.created_at), "started_at": _iso(task.started_at),
        "completed_at": _iso(task.completed_at),
    }
