"""Notification service and Telegram adapter (§111).

    Company Event -> NotificationService -> TelegramAdapter -> Founder

Two rules drive the design:

1. The bot token must never reach a log, an exception message, a task record or git.
   The Telegram API puts the token in the URL path, so any httpx error is a leak
   vector. Every outbound error is scrubbed through `_redact` before it is stored.
2. Routine success must not page the founder (§111). A durable scope state, rather
   than a time window, decides whether a critical condition merits an interruption.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.enums import IncidentStatus, Severity

log = logging.getLogger("company.notify")
RECOVERY_RETRY_STATE_PREFIX = "watchdog-recovery-retry:"

def silence_http_logs() -> None:
    """The Telegram bot token is part of the request URL, and httpx logs every request URL at INFO. The root logger
    runs at INFO (app/main.py, app/worker.py), so without this each send wrote `POST .../bot<TOKEN>/sendMessage` to
    the container logs. Called at import so it holds in every process that can send (API, worker, watchdog)."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


silence_http_logs()

TELEGRAM_API = "https://api.telegram.org"
_REDACTED = "[REDACTED_TOKEN]"

_SEVERITY_PREFIX = {
    Severity.CRITICAL: "CRITICAL",
    Severity.HIGH: "HIGH",
    Severity.MEDIUM: "WARN",
    Severity.LOW: "INFO",
    Severity.INFO: "INFO",
}

_CRITICAL = {
    "PRODUCTION_AUTOMATION_STOPPED", "YOUTUBE_PIPELINE_STOPPED", "SERVICE_DOWN",
    "BACKUP_FAILED", "RESTORE_VERIFICATION_FAILED",
    "MAIN_UNEXPECTED_CHANGE",   # B5: main moved outside a founder-merged PR (app/main_guard.py)
}
_ACTION = {"SCHEDULED_PRODUCTION_MISSING", "WORKER_UNHEALTHY",
           "TASK_FAILED_REPEATEDLY", "APPROVAL_REQUIRED", "MAIN_GUARD_DEGRADED"}
_RANK = {Severity.INFO: 0, Severity.LOW: 1, Severity.MEDIUM: 2,
         Severity.HIGH: 3, Severity.CRITICAL: 4}


def _redact(text: str) -> str:
    """Strip the bot token from any string before it is logged or persisted."""
    token = settings.telegram_bot_token
    if token:
        text = text.replace(token, _REDACTED)
        # httpx sometimes reports only the path segment, e.g. /bot<token>/sendMessage
        text = text.replace(f"bot{token}", f"bot{_REDACTED}")
    return text


def fingerprint(event_type: str, source: str, detail: str) -> str:
    return hashlib.sha256(f"{event_type}|{source}|{detail}".encode()).hexdigest()[:32]


class TelegramAdapter:
    """Thin transport. Knows nothing about company semantics."""

    name = "telegram"

    @property
    def configured(self) -> bool:
        token, ids = settings.telegram_bot_token, settings.chat_ids
        return bool(re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token)
                    and ids and all(re.fullmatch(r"-?[0-9]+", chat) for chat in ids))

    def send(self, text: str) -> None:
        if not self.configured:
            raise RuntimeError("Telegram adapter not configured (token or chat ids missing)")
        url = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}/sendMessage"
        errors: list[str] = []
        for number, chat_id in enumerate(settings.chat_ids, start=1):
            try:
                r = httpx.post(
                    url,
                    json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                    timeout=15.0,
                )
                if r.status_code != 200:
                    # Telegram's error body does not contain the token, but scrub anyway.
                    errors.append(f"recipient {number}: HTTP {r.status_code}")
                elif r.json().get("ok") is not True:
                    errors.append(f"recipient {number}: Telegram did not confirm delivery")
            except Exception as exc:  # noqa: BLE001 - transport errors carry the URL
                errors.append(_redact(f"recipient {number}: {type(exc).__name__}: {exc}"))
        if errors:
            raise RuntimeError("; ".join(errors))


def render(event: models.Event, extra: dict | None = None) -> str:
    """Founder-facing text. Never copy arbitrary payload or agent text to a chat."""
    prefix = _SEVERITY_PREFIX.get(event.severity, "INFO")
    known = _CRITICAL | {"INCIDENT_CREATED", "INCIDENT_RESOLVED", "CONDITION_RECOVERED",
                         "WATCHDOG_CONDITION_RECOVERED",
                         "WORKER_UNHEALTHY", "DISK_CRITICAL"}
    event_name = event.event_type if event.event_type in known else "CRITICAL_EVENT"
    lines = [f"[{prefix}] {event_name}", "Open Founder Desk for details."]
    if event.incident_id:
        lines.append(f"incident id: {event.incident_id}")
    if extra:
        lines += [f"{k}: {v}" for k, v in extra.items()]
    lines.append(f"at: {event.occurred_at.isoformat()}")
    return "\n".join(lines)


class NotificationService:
    def __init__(self, adapter: TelegramAdapter | None = None) -> None:
        self.adapter = adapter or TelegramAdapter()

    def category(self, event: models.Event) -> str:
        if event.event_type in ("INCIDENT_RESOLVED", "WATCHDOG_CONDITION_RECOVERED"):
            return "recovery"
        if event.severity == Severity.CRITICAL:
            return "critical"
        if event.event_type == "DISK_CRITICAL":
            return "action_required"  # 85% is a warning; 95% is CRITICAL.
        if event.event_type == "INCIDENT_CREATED":
            return "critical" if event.severity == Severity.HIGH else "action_required"
        if event.event_type == "WORKER_UNHEALTHY":
            condition = (event.payload or {}).get("condition_key")
            return "critical" if condition == "worker-heartbeat" else "action_required"
        if event.event_type in _CRITICAL:
            return "critical"
        if event.event_type in _ACTION:
            return "action_required"
        return "informational"

    def should_notify(self, event: models.Event) -> bool:
        return self.category(event) == "critical"

    @staticmethod
    def delivered_incident_ids(db: Session, incidents: list[models.Incident]) -> set[int]:
        """Return incidents with persisted delivery evidence, including legacy rows.

        `founder_notified` predates notification-state persistence and can be stale.
        Prefer the event/state records as evidence without mutating data during reads.
        """
        incident_ids = {incident.id for incident in incidents if incident.id is not None}
        delivered = {incident.id for incident in incidents
                     if incident.id is not None and incident.founder_notified}
        unresolved_ids = incident_ids - delivered
        if not unresolved_ids:
            return delivered

        scopes = {f"incident:{incident_id}": incident_id for incident_id in unresolved_ids}
        sent_scopes = set(db.scalars(select(models.NotificationState.scope_key).where(
            models.NotificationState.scope_key.in_(scopes),
            models.NotificationState.delivery_status == "sent",
        )).all())
        delivered.update(scopes[scope] for scope in sent_scopes)
        delivered.update(db.scalars(select(models.Event.incident_id).where(
            models.Event.incident_id.in_(unresolved_ids),
            models.Event.notified.is_(True),
        )).all())
        return delivered

    @staticmethod
    def scope(event: models.Event) -> str:
        if event.incident_id:
            return f"incident:{event.incident_id}"
        if event.service_id and event.event_type in (
            "SERVICE_DOWN", "YOUTUBE_PIPELINE_STOPPED", "PRODUCTION_AUTOMATION_STOPPED",
            "SCHEDULED_PRODUCTION_MISSING",
        ):
            return f"service:{event.service_id}:production"
        # The watchdog supplies a stable condition key. Its human-readable title and
        # changing heartbeat age/task count must never enter the incident identity.
        condition = (event.payload or {}).get("condition_key")
        raw = f"{event.source}|{condition or event.event_type}|{event.service_id or ''}"
        return "condition:" + hashlib.sha256(raw.encode()).hexdigest()[:40]

    @staticmethod
    def _record(db: Session | None, event: models.Event, reason: str,
                error: str | None = None) -> None:
        event.notify_reason = reason
        event.notify_error = error
        if db is not None:
            db.commit()

    @staticmethod
    def _locked_state(db: Session, scope: str) -> models.NotificationState:
        # INSERT ON CONFLICT works on PostgreSQL and SQLite. The unique scope row is
        # created before SELECT FOR UPDATE, including under concurrent first sightings.
        db.execute(text("INSERT INTO notification_states "
                        "(scope_key, active, episode, delivery_status) "
                        "VALUES (:scope, true, 1, 'idle') ON CONFLICT (scope_key) DO NOTHING"),
                   {"scope": scope})
        return db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == scope).with_for_update())

    @staticmethod
    def _pending_watchdog_recovery(db: Session, event_id: int, episode: int) -> str | None:
        """Return the durable in-flight/ambiguous recovery state for this episode."""
        recoveries = db.scalars(select(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED",
            models.Event.source == "watchdog",
            models.Event.notify_reason.in_(("recovery dispatch pending", "recovery delivery pending")),
        )).all()
        for recovery in recoveries:
            payload = recovery.payload or {}
            if (payload.get("resolved_event_id") == event_id
                    and payload.get("episode") == episode):
                return recovery.notify_reason
        return None

    @staticmethod
    def _related_recovery_events(db: Session, event: models.Event):
        waiting = ("recovery waiting for alert delivery",
                   "recovery blocked by uncertain alert delivery")
        if event.source == "watchdog" and event.id is not None:
            return db.scalars(select(models.Event).where(
                models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED",
                models.Event.source == "watchdog",
                models.Event.payload["resolved_event_id"].as_integer() == event.id,
                models.Event.notify_reason.in_(waiting),
            )).all()
        if event.incident_id is not None:
            return db.scalars(select(models.Event).where(
                models.Event.event_type == "INCIDENT_RESOLVED",
                models.Event.incident_id == event.incident_id,
                models.Event.notify_reason.in_(waiting),
            )).all()
        return []

    @classmethod
    def _wake_waiting_recoveries(cls, db: Session, event: models.Event) -> None:
        for recovery in cls._related_recovery_events(db, event):
            recovery.notify_reason = "recovery dispatch pending"
            recovery.notify_error = None
            checkpoint = db.get(models.ControlState, f"{RECOVERY_RETRY_STATE_PREFIX}{recovery.id}")
            if checkpoint is not None:
                db.delete(checkpoint)

    @classmethod
    def _block_waiting_recoveries(cls, db: Session, event: models.Event) -> None:
        for recovery in cls._related_recovery_events(db, event):
            recovery.notify_reason = "recovery blocked by uncertain alert delivery"
            recovery.notify_error = "Source alert delivery is uncertain; founder review is required."

    def dispatch(self, db: Session, event: models.Event) -> tuple[bool, str | None]:
        """Returns (delivered, reason_not_delivered). Never raises — a broken notifier
        must not break the caller that recorded the event. The reason is a fixed category: API routes return it,
        so exception text (even redacted) stays in notify_error and the log, never in a response."""
        try:
            event_type = event.event_type
            event_id = event.id
        except Exception as exc:  # detached/expired objects cannot be safely classified
            log.error("notification event identity unavailable: %s", type(exc).__name__)
            return False, "event identity unavailable"
        try:
            return self._dispatch(db, event)
        except Exception as exc:  # noqa: BLE001 - protect the already recorded event
            if db is not None:
                try:
                    db.rollback()
                except Exception as rollback_exc:
                    log.error("notification rollback failed: %s", type(rollback_exc).__name__)
            msg = _redact(f"{type(exc).__name__}: {exc}")[:900]
            # Recovery intent is committed separately before Telegram I/O. If the
            # final state commit fails after Telegram accepted the message, preserve
            # that durable ambiguity instead of replacing it with a generic error.
            if event_type in ("WATCHDOG_CONDITION_RECOVERED", "INCIDENT_RESOLVED"):
                if event_id is None or db is None:
                    log.error("recovery delivery state is unknown: %s", msg)
                    return False, "recovery delivery state unknown"
                try:
                    with Session(bind=db.get_bind()) as marker_db:
                        marker = marker_db.execute(select(
                            models.Event.notify_reason, models.Event.notified
                        ).where(models.Event.id == event_id)).one_or_none()
                        if marker is None or marker.notify_reason is not None or marker.notified:
                            log.error("recovery delivery state preserved: %s", msg)
                            return False, "recovery delivery state preserved"
                        # A NULL marker proves the recovery send intent was never
                        # persisted, so the failure happened before Telegram I/O.
                        marker_db.execute(update(models.Event).where(
                            models.Event.id == event_id,
                            models.Event.notify_reason.is_(None),
                            models.Event.notified.is_(False),
                        ).values(notify_reason="recovery dispatch pending", notify_error=msg))
                        marker_db.commit()
                    log.error("recovery dispatch remains retryable: %s", msg)
                    return False, "recovery dispatch pending"
                except Exception:  # the durable marker cannot be disproved safely
                    log.error("recovery delivery state is unknown: %s", msg)
                    return False, "recovery delivery state unknown"
            try:
                if db is not None:
                    event.notify_error = msg
                    event.notify_reason = "notification decision failed"
                    db.commit()
            except Exception:  # schema drift may prevent recording the reason
                if db is not None:
                    try:
                        db.rollback()
                    except Exception as rollback_exc:
                        log.error("notification fallback rollback failed: %s",
                                  type(rollback_exc).__name__)
            log.error("notification decision failed: %s", msg)
            return False, "notification decision failed"

    def _dispatch(self, db: Session, event: models.Event) -> tuple[bool, str | None]:
        category = self.category(event)
        if category == "recovery":
            return self._recovery(db, event)
        if category != "critical":
            reason = "action required in Desk" if category == "action_required" else "informational only"
            self._record(db, event, reason)
            return False, reason
        if db is None:
            return False, "database unavailable"
        scope = self.scope(event)
        state = self._locked_state(db, scope)
        payload = event.payload or {}
        watchdog_episode = payload.get("condition_episode")
        is_watchdog_condition = (
            event.source == "watchdog"
            and isinstance(payload.get("condition_scope"), str)
            and payload["condition_scope"].startswith("watchdog-condition:")
            and isinstance(watchdog_episode, int)
            and not isinstance(watchdog_episode, bool)
            and watchdog_episode > 0
        )
        # A new watchdog episode may be dispatched before the previous episode's
        # recovery event. Do not let the old notification scope suppress its alert.
        previous_watchdog_episode = state.episode
        if is_watchdog_condition and state.event_id:
            previous_event = db.get(models.Event, state.event_id)
            previous_payload = previous_event.payload or {} if previous_event else {}
            previous_watchdog_episode = previous_payload.get("condition_episode", state.episode)
        pending_recovery = (self._pending_watchdog_recovery(
            db, state.event_id, previous_watchdog_episode)
            if is_watchdog_condition and state.active and state.event_id else None)
        if pending_recovery is not None:
            # A process can die before or after Telegram accepts a recovery. Never let
            # a newer alert overtake the durable recovery decision. Dispatch-pending
            # remains safely redrivable; delivery-pending is ambiguous and latches the
            # notification state for explicit human review.
            if pending_recovery == "recovery delivery pending":
                state.delivery_status = "uncertain"
                reason = "prior recovery delivery uncertain; manual review required"
            else:
                reason = "prior recovery delivery pending; automatic retry required"
            self._record(db, event, reason)
            return False, reason
        if (is_watchdog_condition and state.active
                and isinstance(previous_watchdog_episode, int)
                and not isinstance(previous_watchdog_episode, bool)
                and watchdog_episode > previous_watchdog_episode):
            if state.delivery_status == "uncertain":
                reason = "prior condition delivery uncertain; manual review required"
                self._record(db, event, reason)
                return False, reason
            state.active = False
            state.delivery_status = "recovered"
        elif (is_watchdog_condition
              and isinstance(previous_watchdog_episode, int)
              and not isinstance(previous_watchdog_episode, bool)
              and watchdog_episode < previous_watchdog_episode):
            # Delayed work from an older episode must not replace the event ID for
            # the current episode. Its recovery is keyed to that ID and would then
            # be unable to close the active notification state.
            reason = "stale watchdog condition episode"
            self._record(db, event, reason)
            return False, reason
        if not state.active:
            state.active = True
            state.episode = max(state.episode + 1, watchdog_episode or 0)
            state.severity, state.delivery_status = None, "idle"
        if state.delivery_status in ("pending", "uncertain"):
            reason = "delivery pending or uncertain; manual review required"
            self._record(db, event, reason)
            return False, reason
        if state.delivery_status == "sent" and state.severity is not None:
            previous = Severity(state.severity)
            if _RANK[event.severity] <= _RANK[previous]:
                reason = "active incident already notified"
                self._record(db, event, reason)
                return False, reason
        if not self.adapter.configured:
            reason = "Telegram adapter not configured"
            self._record(db, event, reason, reason)
            return False, reason
        reason = "severity escalation" if state.delivery_status == "sent" else "new critical incident"
        state.delivery_status = "pending"
        state.event_id = event.id
        state.attempted_at = datetime.now(timezone.utc)
        claim_episode = state.episode
        self._record(db, event, "delivery pending")
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == scope).with_for_update()
            .execution_options(populate_existing=True))
        if (state is None or not state.active or state.event_id != event.id
                or state.episode != claim_episode or state.delivery_status != "pending"):
            reason = "notification superseded before send"
            self._record(db, event, reason)
            return False, reason
        db.commit()  # release the state lock before network I/O
        try:
            self.adapter.send(render(event))
        except Exception as exc:  # noqa: BLE001
            msg = _redact(f"{type(exc).__name__}: {exc}")[:900]
            # An exception may follow a successful remote send (including a partial
            # multi-chat send). Do not retry automatically and create duplicate pages.
            state = db.scalar(select(models.NotificationState).where(
                models.NotificationState.scope_key == scope).with_for_update()
                .execution_options(populate_existing=True))
            if (state is not None and state.active and state.event_id == event.id
                    and state.episode == claim_episode and state.delivery_status == "pending"):
                state.delivery_status = "uncertain"
            self._block_waiting_recoveries(db, event)
            self._record(db, event, "delivery failed or uncertain", msg)
            log.error("telegram delivery failed: %s", msg)
            return False, "delivery failed or uncertain"
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == scope).with_for_update()
            .execution_options(populate_existing=True))
        state_is_current = bool(
            state is not None and state.active and state.event_id == event.id
            and state.episode == claim_episode and state.delivery_status == "pending"
        )
        if state_is_current:
            state.delivery_status = "sent"
            state.severity = event.severity.value
            state.delivered_at = datetime.now(timezone.utc)
        event.notified = True
        if event.incident_id:
            incident = db.get(models.Incident, event.incident_id)
            if incident is not None:
                incident.founder_notified = True
        self._wake_waiting_recoveries(db, event)
        self._record(db, event, reason if state_is_current else "delivered after a newer condition episode began")
        return True, None

    def _recovery(self, db: Session, event: models.Event) -> tuple[bool, str | None]:
        if not event.incident_id and not (event.payload or {}).get("condition_key"):
            self._record(db, event, "recovery without incident scope")
            return False, "recovery without incident scope"
        if event.event_type == "INCIDENT_RESOLVED":
            incident = db.get(models.Incident, event.incident_id)
            if incident is None or incident.status != IncidentStatus.RESOLVED:
                self._record(db, event, "incident not resolved")
                return False, "incident not resolved"
        scope = self.scope(event)
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == scope).with_for_update()
            .execution_options(populate_existing=True))
        # The notification-state row serializes recoveries and new episodes for the
        # same scope. Do not lock the event row: PostgreSQL persists the pending
        # intent on a second connection while this transaction keeps the scope lock
        # across Telegram I/O. That preserves send order without losing crash evidence.
        event = db.scalar(select(models.Event).where(
            models.Event.id == event.id).execution_options(populate_existing=True))
        if event is None:
            return False, "recovery event disappeared"
        if (event.notified or event.notify_reason not in
                (None, "recovery dispatch pending")):
            return event.notified, event.notify_reason or "recovery already delivered"
        payload = event.payload or {}
        if event.event_type == "WATCHDOG_CONDITION_RECOVERED":
            resolved_event_id = payload.get("resolved_event_id")
            resolved_episode = payload.get("episode")
            resolved_event = (db.get(models.Event, resolved_event_id)
                              if isinstance(resolved_event_id, int)
                              and not isinstance(resolved_event_id, bool) else None)
            resolved_payload = resolved_event.payload or {} if resolved_event else {}
            if (not isinstance(resolved_event_id, int) or isinstance(resolved_event_id, bool)
                    or not isinstance(resolved_episode, int) or isinstance(resolved_episode, bool)
                    or (state is not None and state.event_id != resolved_event_id)
                    or resolved_payload.get("condition_episode") != resolved_episode
                    or resolved_payload.get("condition_scope") != payload.get("condition_scope")
                    or resolved_payload.get("condition_key") != payload.get("condition_key")):
                self._record(db, event, "stale recovery does not match the active condition episode")
                return False, "stale recovery does not match the active condition episode"
        if state is None or not state.active:
            self._record(db, event, "incident was not actively notified")
            return False, "incident was not actively notified"
        if state.delivery_status == "pending":
            # The original alert may still be in Telegram I/O. Keep the recovery
            # intent durable but out of the polling queue. Its completion path wakes
            # it only after delivery reaches a known outcome.
            self._record(db, event, "recovery waiting for alert delivery")
            return False, "alert delivery pending; recovery remains retryable"
        if state.delivery_status == "uncertain":
            # The alert may have reached the founder. Do not claim recovery or send
            # another message until a human resolves this ambiguous delivery.
            self._record(db, event, "recovery blocked by uncertain alert delivery")
            return False, "alert delivery uncertain; manual review required"
        was_sent = state.delivery_status == "sent"
        if not was_sent or not self.adapter.configured:
            state.active = False
            state.delivery_status = "recovered"
            self._record(db, event, "incident recovered without Telegram delivery")
            return False, "incident recovered without Telegram delivery"
        # Persist an explicit intent before I/O. A crash after this commit is ambiguous:
        # leave it visible for founder review and never auto-send a duplicate recovery.
        # On PostgreSQL, use a separate transaction so this Session retains the scope
        # row lock until delivery is resolved. Other test/local dialects have no row
        # lock semantics, so persist through the current Session.
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            with Session(bind=bind) as intent_db:
                intent_db.execute(update(models.Event).where(
                    models.Event.id == event.id).values(
                        notify_reason="recovery delivery pending", notify_error=None))
                intent_db.commit()
            db.expire(event, ["notify_reason", "notify_error"])
        else:
            event.notify_reason = "recovery delivery pending"
            event.notify_error = None
            db.commit()
        try:
            self.adapter.send(render(event, {"status": "recovered"}))
        except Exception as exc:  # noqa: BLE001
            msg = _redact(f"{type(exc).__name__}: {exc}")[:900]
            state.active = True
            state.delivery_status = "uncertain"
            self._record(db, event, "recovery delivery failed or uncertain", msg)
            return False, "recovery delivery failed or uncertain"
        state.active = False
        state.delivery_status = "recovered"
        event.notified = True
        self._record(db, event, "recovery delivered")
        return True, None

    def recover_condition(self, db: Session, *, source: str, condition_key: str) -> bool:
        """Close a condition only after its caller has positively verified recovery."""
        try:
            probe = models.Event(event_type="CONDITION_RECOVERED", source=source,
                                 severity=Severity.INFO, payload={"condition_key": condition_key})
            scope = self.scope(probe)
            state = db.get(models.NotificationState, scope)
            if state is None or not state.active:
                return False
            db.add(probe)
            db.commit()
            delivered, _ = self._recovery(db, probe)
            return delivered
        except Exception as exc:  # noqa: BLE001 - recovery must not break monitoring
            db.rollback()
            log.error("notification recovery failed: %s", _redact(f"{type(exc).__name__}: {exc}")[:900])
            return False

    def retry_uncertain(self, db: Session, event: models.Event) -> tuple[bool, str | None]:
        """A founder may explicitly retry an ambiguous send after the transport settles."""
        if self.category(event) == "recovery":
            return self.retry_recovery(db, event)
        if self.category(event) != "critical":
            raise ValueError("only critical notifications can be retried")
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == self.scope(event)).with_for_update())
        if (state is None or not state.active or state.event_id != event.id
                or state.delivery_status not in ("pending", "uncertain")):
            raise ValueError("no uncertain delivery for this event")
        attempted = state.attempted_at
        if attempted is None or (datetime.now(timezone.utc) -
                                 (attempted if attempted.tzinfo else attempted.replace(tzinfo=timezone.utc))) < timedelta(minutes=5):
            raise ValueError("the current delivery may still be in flight")
        state.delivery_status = "idle"
        db.commit()
        return self.dispatch(db, event)

    def retry_recovery(self, db: Session, event: models.Event) -> tuple[bool, str | None]:
        """Founder-only API callers may resume exhausted, proven pre-send recoveries."""
        if event.event_type not in ("INCIDENT_RESOLVED", "WATCHDOG_CONDITION_RECOVERED"):
            raise ValueError("only incident recovery notifications can be resumed")
        if (event.notified or event.notify_reason !=
                "recovery dispatch retries exhausted; founder review required"):
            raise ValueError("no exhausted recovery delivery is awaiting review")
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == self.scope(event)).with_for_update())
        if (state is None or not state.active or state.delivery_status != "sent"):
            raise ValueError("source alert must be definitively delivered before recovery retry")
        if event.event_type == "INCIDENT_RESOLVED":
            incident = db.get(models.Incident, event.incident_id) if event.incident_id else None
            if incident is None or incident.status != IncidentStatus.RESOLVED:
                raise ValueError("incident must remain resolved before recovery retry")
        else:
            payload = event.payload or {}
            if (payload.get("resolved_event_id") != state.event_id
                    or payload.get("episode") != state.episode):
                raise ValueError("recovery must match the currently active condition episode")
        event.notify_reason = "recovery dispatch pending"
        event.notify_error = None
        checkpoint = db.get(models.ControlState, f"{RECOVERY_RETRY_STATE_PREFIX}{event.id}")
        if checkpoint is not None:
            db.delete(checkpoint)
        db.commit()
        return self.dispatch(db, event)


notifier = NotificationService()
