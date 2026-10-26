"""Event -> incident -> task, by deterministic rules (§62, §93, §150).

An alert alone leaves the founder as the scheduler: someone still has to notice the
Telegram message and open work. This module closes that gap without a model:

    event -> rule? -> open incident for the same scope?  yes -> nothing new
                                                     no  -> incident + READY task

One open incident per scope is the dedupe. An incident someone already opened keeps its
existing owner and task, so routing never duplicates work a human started (INC-0001 /
TASK-0002 are exactly that case). Tasks are not opted in to the unattended worker:
fixing a stopped service changes production, which stays on the attended path until
Stage C (docs/AUTONOMOUS_AGENT_ARCHITECTURE.md §7).
"""
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, enums, keys, models
from app.notify import fingerprint, notifier

log = logging.getLogger("company.event_router")


@dataclass(frozen=True)
class Rule:
    severity: enums.IncidentSeverity
    agent: str
    priority: enums.Priority
    title: str              # "{scope}" is the service key, or "host" without one
    objective: str
    success_criteria: str
    needs_service: bool = True


_PRODUCTION = Rule(
    enums.IncidentSeverity.SEV1, "developer", enums.Priority.P0,
    "{scope}: production stopped",
    "Find why {scope} stopped producing and restore it without duplicate output.",
    "Root cause recorded; one item produced end to end; no duplicates; incident "
    "resolved with a learning.",
)

# Deliberately small. BACKUP_FAILED is not routed yet: INC-0002 / TASK-0001 already own
# the backup gap, and a host-scoped rule would open a parallel incident for it.
RULES: dict[str, Rule] = {
    "YOUTUBE_PIPELINE_STOPPED": _PRODUCTION,
    "PRODUCTION_AUTOMATION_STOPPED": _PRODUCTION,
    # A missed publish needs investigation; a verified stopped pipeline is urgent.
    "SCHEDULED_PRODUCTION_MISSING": replace(_PRODUCTION, severity=enums.IncidentSeverity.SEV2),
    "SERVICE_DOWN": Rule(
        enums.IncidentSeverity.SEV1, "developer", enums.Priority.P1,
        "{scope}: service down",
        "Restore {scope} and find why it went down.",
        "Service healthy again with evidence; root cause recorded; incident resolved.",
    ),
    "DISK_CRITICAL": Rule(
        enums.IncidentSeverity.SEV1, "developer", enums.Priority.P0,
        "{scope}: disk critical",
        "Free disk space safely on {scope} without deleting unknown data (§23).",
        "Disk below the high threshold; what was removed is listed; nothing unknown deleted.",
        needs_service=False,
    ),
}

_EVENT_SEVERITY = {
    enums.IncidentSeverity.SEV0: enums.Severity.CRITICAL,
    enums.IncidentSeverity.SEV1: enums.Severity.HIGH,
    enums.IncidentSeverity.SEV2: enums.Severity.MEDIUM,
    enums.IncidentSeverity.SEV3: enums.Severity.LOW,
}
_INCIDENT_RANK = {enums.IncidentSeverity.SEV0: 0, enums.IncidentSeverity.SEV1: 1,
                  enums.IncidentSeverity.SEV2: 2, enums.IncidentSeverity.SEV3: 3}


def _inherit_source_notification(db: Session, event: models.Event, incident_id: int,
                                source_scope: str | None) -> bool:
    """Carry prior watchdog delivery evidence into the new incident scope on redrive."""
    if source_scope is None:
        return False
    source = db.get(models.NotificationState, source_scope)
    if source is None:
        return False
    delivered = event.notified or source.delivery_status == "sent"
    status = "sent" if delivered else source.delivery_status
    if status not in ("sent", "pending", "uncertain"):
        return False

    target = notifier._locked_state(db, f"incident:{incident_id}")
    if target.delivery_status != "sent":
        target.active = True
        target.event_id = event.id
        target.severity = source.severity or event.severity.value
        target.delivery_status = status
        target.attempted_at = source.attempted_at
        target.delivered_at = source.delivered_at if delivered else None
    incident = db.get(models.Incident, incident_id)
    if incident is not None and (delivered or target.delivery_status == "sent"):
        incident.founder_notified = True
    db.commit()
    return True


def route(db: Session, event: models.Event) -> dict | None:
    """Returns what was done, or None when no rule applies."""
    rule = RULES.get(event.event_type)
    if rule is None:
        return None
    payload = event.payload or {}
    source_notification_scope = notifier.scope(event) if event.source == "watchdog" else None
    # Watchdog routing is allowed only while this exact condition episode still owns
    # the durable processing claim. Holding this row lock through the incident/task
    # commit serializes routing against recovery and recurrence.
    if event.source == "watchdog" and isinstance(payload.get("condition_scope"), str):
        scope = payload["condition_scope"]
        episode = payload.get("condition_episode")
        claim = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == scope).with_for_update())
        if (claim is None or not claim.active or claim.event_id != event.id
                or claim.delivery_status != "processing"
                or not isinstance(episode, int) or isinstance(episode, bool)
                or claim.episode != episode):
            return None

    service_id = event.service_id
    service_key = payload.get("service_key")
    if service_id is None and isinstance(service_key, str):
        service_id = db.scalar(select(models.Service.id).where(models.Service.key == service_key))
    service = db.get(models.Service, service_id) if service_id else None
    if rule.needs_service and service is None:
        return None
    scope = service.key if service else "host"
    title = rule.title.format(scope=scope)
    incident_severity = rule.severity
    if event.event_type == "DISK_CRITICAL":
        incident_severity = (enums.IncidentSeverity.SEV0 if event.severity == enums.Severity.CRITICAL
                             else enums.IncidentSeverity.SEV2)

    open_incident = models.Incident.status != enums.IncidentStatus.RESOLVED
    same_scope = (models.Incident.service_id == service.id) if service else (models.Incident.title == title)
    existing = db.execute(select(models.Incident).where(open_incident, same_scope)
                          .order_by(models.Incident.id).limit(1)).scalar_one_or_none()
    if existing:
        if _INCIDENT_RANK[incident_severity] < _INCIDENT_RANK[existing.severity]:
            existing.severity = incident_severity
        event.incident_id = existing.id
        db.commit()
        _inherit_source_notification(db, event, existing.id, source_notification_scope)
        return {"event": event.event_type, "incident": existing.key, "created": False}

    # Same meaning as the SELECT above, enforced by uq_incidents_open_scope for concurrent events.
    scope_key = (f"service:{service.id}" if service else f"title:{title}")[:300]
    incident = models.Incident(
        severity=incident_severity, title=title, open_scope=scope_key,
        description=f"{payload.get('title', event.event_type)}. {payload.get('detail', '')}".strip()[:2000],
        started_at=event.occurred_at or datetime.now(timezone.utc),
        detected_by=event.source, service_id=service.id if service else None,
    )
    try:
        keys.add_with_key(db, incident, "INC")
    except keys.DuplicateOpenWork:  # a concurrent event opened it after our check
        # add_with_key uses a SAVEPOINT, so preserve the watchdog claim lock while
        # resolving the already-open incident.
        existing = db.execute(select(models.Incident).where(
            models.Incident.open_scope == scope_key,
            models.Incident.status != enums.IncidentStatus.RESOLVED)).scalar_one()
        event.incident_id = existing.id
        db.commit()
        _inherit_source_notification(db, event, existing.id, source_notification_scope)
        return {"event": event.event_type, "incident": existing.key, "created": False}
    agent = db.execute(select(models.Agent).where(models.Agent.key == rule.agent)).scalar_one_or_none()
    unit_id = service.business_unit_id if service else None
    task = models.Task(
        title=title,
        description=f"Opened automatically from {event.event_type} (event {event.id}).",
        objective=rule.objective.format(scope=scope), success_criteria=rule.success_criteria,
        status=enums.TaskStatus.READY, priority=rule.priority, risk_level=enums.RiskLevel.MEDIUM,
        assigned_agent_id=agent.id if agent else None, business_unit_id=unit_id,
        created_by="event-router", dedupe_key=f"incident:{incident.key}",
        context={"incident": incident.key, "source_event": event.id, "autonomous": False},
    )
    keys.add_with_key(db, task, "TASK")
    event.incident_id = incident.id
    audit.record(db, actor="event-router", action="incident.create", entity_type="incident",
                 entity_key=incident.key, reason=f"rule {event.event_type} on {scope}",
                 after=audit.snapshot(incident), task_id=task.id)
    db.commit()

    alert = models.Event(
        event_type="INCIDENT_CREATED", source="event-router",
        severity=_EVENT_SEVERITY[incident_severity], service_id=incident.service_id,
        incident_id=incident.id, task_id=task.id,
        payload={"title": f"{incident.key} opened, {task.key} assigned to {rule.agent}: {title}",
                 "message": incident.description[:400]},
        dedupe_key=fingerprint("INCIDENT_CREATED", "event-router", incident.key),
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    source_state = db.get(models.NotificationState, source_notification_scope) if source_notification_scope else None
    source_already_alerted = bool(
        event.notified or (source_state and source_state.delivery_status in ("sent", "pending", "uncertain"))
    )
    if source_already_alerted:
        _inherit_source_notification(db, event, incident.id, source_notification_scope)
        alert.notify_reason = "source watchdog alert already sent or delivery is ambiguous"
        db.commit()
    else:
        notifier.dispatch(db, alert)
    return {"event": event.event_type, "incident": incident.key, "task": task.key,
            "created": True}


def safe_route(db: Session, event: models.Event) -> dict | None:
    """Routing must never lose the event that triggered it."""
    try:
        return route(db, event)
    except Exception:
        db.rollback()
        log.exception("event routing failed for event %s", event.id)
        return None
