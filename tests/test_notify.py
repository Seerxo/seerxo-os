"""Tests for the notification adapter.

The token-redaction tests are the important ones: the Telegram API puts the bot token in
the URL path, so any transport error is a leak vector. A regression here would publish a
credential into logs and the events table.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import enums, main, models, notify
from app.config import settings

TOKEN = "123456789:FAKE_TEST_TOKEN_not_a_real_credential"


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(settings, "telegram_admin_chat_ids", "111,222")
    return TOKEN


def test_redact_removes_bare_token(token):
    # Deliberately WITHOUT the "bot" prefix. An earlier version of this test used a full
    # Telegram URL, which meant the second replace in _redact covered for the first and
    # the bare-token path was never exercised — mutation testing caught that.
    leaked = f"config dump: TELEGRAM_BOT_TOKEN={token} (do not log)"
    out = notify._redact(leaked)
    assert token not in out
    assert "[REDACTED_TOKEN]" in out


def test_redact_removes_token_inside_a_url(token):
    out = notify._redact(f"HTTPError at https://api.telegram.org/bot{token}/sendMessage")
    assert token not in out


def test_redact_covers_a_partial_token_fragment(token):
    # httpx sometimes truncates; the secret portion must still not survive intact.
    out = notify._redact(f"timeout contacting {token}")
    assert token not in out


def test_redact_handles_bot_prefixed_form(token):
    out = notify._redact(f"connect failed: bot{token}/sendMessage")
    assert token not in out


def test_redact_is_safe_without_a_token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    assert notify._redact("nothing to hide") == "nothing to hide"


def test_adapter_is_unconfigured_without_chat_ids(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(settings, "telegram_admin_chat_ids", "")
    assert notify.TelegramAdapter().configured is False


def test_adapter_rejects_malformed_chat_or_token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(settings, "telegram_admin_chat_ids", "111,not-a-chat")
    assert notify.TelegramAdapter().configured is False
    monkeypatch.setattr(settings, "telegram_admin_chat_ids", "111")
    monkeypatch.setattr(settings, "telegram_bot_token", "bad/token")
    assert notify.TelegramAdapter().configured is False


def test_send_refuses_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    with pytest.raises(RuntimeError, match="not configured"):
        notify.TelegramAdapter().send("x")


def test_send_scrubs_token_from_transport_errors(token, monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError(f"failed to reach https://api.telegram.org/bot{token}/sendMessage")

    monkeypatch.setattr(notify.httpx, "post", boom)
    with pytest.raises(RuntimeError) as exc:
        notify.TelegramAdapter().send("hello")
    assert TOKEN not in str(exc.value)
    assert "[REDACTED_TOKEN]" in str(exc.value)


def test_send_treats_non_200_as_failure(token, monkeypatch):
    class Resp:
        status_code = 400
        text = '{"ok":false,"description":"chat not found"}'

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **kw: Resp())
    with pytest.raises(RuntimeError, match="HTTP 400"):
        notify.TelegramAdapter().send("hello")


def test_http_200_without_telegram_confirmation_is_not_delivery(token, monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"ok": False}

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **kw: Resp())
    with pytest.raises(RuntimeError, match="did not confirm delivery"):
        notify.TelegramAdapter().send("hello")


def test_fingerprint_is_stable_and_distinct():
    a = notify.fingerprint("X", "src", "detail")
    assert a == notify.fingerprint("X", "src", "detail")
    assert a != notify.fingerprint("X", "src", "other")
    assert len(a) == 32


class _Adapter:
    """Records sends instead of performing them."""

    configured = True

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def _event(event_type: str, dedupe: str | None = "k") -> models.Event:
    from datetime import datetime, timezone
    e = models.Event(event_type=event_type, source="test", severity=enums.Severity.HIGH,
                     payload={"title": "t"}, dedupe_key=dedupe)
    e.occurred_at = datetime.now(timezone.utc)
    e.notified = False
    return e


def test_non_alertable_event_is_not_delivered():
    svc = notify.NotificationService(_Adapter())
    # ROUTINE_THING is not in ALERTABLE_EVENTS, so it must never page the founder (§111).
    delivered, reason = svc.dispatch(None, _event("ROUTINE_THING"))
    assert delivered is False
    assert reason == "informational only"


def test_alertable_event_type_is_recognised():
    svc = notify.NotificationService(_Adapter())
    assert svc.should_notify(_event("BACKUP_FAILED")) is True
    assert svc.should_notify(_event("ROUTINE_THING")) is False


def test_render_keeps_arbitrary_payload_out_of_telegram():
    ev = _event("BACKUP_FAILED")
    ev.payload = {"title": "sensitive detail", "warnings": [f"w{i}" for i in range(9)]}
    text = notify.render(ev)
    assert "BACKUP_FAILED" in text
    assert "sensitive detail" not in text and "w0" not in text


def _db_event(db, event_type="SERVICE_DOWN", severity=enums.Severity.HIGH,
              incident_id=None, condition_key=None):
    ev = models.Event(event_type=event_type, source="watchdog", severity=severity,
                      incident_id=incident_id, payload={"condition_key": condition_key} if condition_key else {})
    db.add(ev)
    db.commit()
    return ev


def test_unresolved_critical_condition_stays_quiet_across_six_hours():
    from datetime import datetime, timedelta, timezone
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        first = _db_event(db)
        assert svc.dispatch(db, first) == (True, None)
        state = db.scalar(select(models.NotificationState))
        state.delivered_at = datetime.now(timezone.utc) - timedelta(hours=7)
        db.commit()
        second = _db_event(db)
        assert svc.dispatch(db, second) == (False, "active incident already notified")
        assert second.notify_reason == "active incident already notified"
        assert len(adapter.sent) == 1


def test_event_types_for_one_incident_send_once_then_escalate():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        inc = models.Incident(key="INC-0001", severity=enums.IncidentSeverity.SEV1,
                              title="service down", description="x", detected_by="watchdog",
                              started_at=notify.datetime.now(notify.timezone.utc))
        db.add(inc)
        db.commit()
        first = _db_event(db, "INCIDENT_CREATED", incident_id=inc.id)
        assert svc.dispatch(db, first)[0] is True
        second = _db_event(db, "SERVICE_DOWN", incident_id=inc.id)
        assert svc.dispatch(db, second)[0] is False
        third = _db_event(db, "SERVICE_DOWN", enums.Severity.CRITICAL, inc.id)
        assert svc.dispatch(db, third)[0] is True
        assert third.notify_reason == "severity escalation"
        assert len(adapter.sent) == 2


def test_successful_delivery_marks_linked_incident_notified():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    svc = notify.NotificationService(_Adapter())
    with Session(engine) as db:
        inc = models.Incident(key="INC-0001", severity=enums.IncidentSeverity.SEV1,
                              title="service down", description="x", detected_by="watchdog",
                              started_at=notify.datetime.now(notify.timezone.utc))
        db.add(inc)
        db.commit()
        event = _db_event(db, "YOUTUBE_PIPELINE_STOPPED", incident_id=inc.id)

        assert svc.dispatch(db, event) == (True, None)

        db.refresh(inc)
        assert event.notified is True
        assert inc.founder_notified is True


def test_incident_api_projects_delivery_evidence_without_mutating_legacy_flag():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        incidents = [models.Incident(
            key=f"INC-000{i}", severity=enums.IncidentSeverity.SEV1,
            title="service down", description="x", detected_by="watchdog",
            started_at=notify.datetime.now(notify.timezone.utc), founder_notified=False,
            status=enums.IncidentStatus.INVESTIGATING,
        ) for i in (1, 2)]
        db.add_all(incidents)
        db.commit()

        delivered_event = _db_event(db, "YOUTUBE_PIPELINE_STOPPED", incident_id=incidents[0].id)
        delivered_event.notified = True
        db.add(models.NotificationState(scope_key=f"incident:{incidents[1].id}",
                                        delivery_status="sent"))
        db.commit()

        response = {item["key"]: item for item in main.list_incidents(db=db)}

        assert response[incidents[0].key]["founder_notified"] is True
        assert response[incidents[1].key]["founder_notified"] is True
        db.refresh(incidents[0])
        db.refresh(incidents[1])
        assert incidents[0].founder_notified is False
        assert incidents[1].founder_notified is False


def test_recovery_closes_occurrence_and_new_occurrence_notifies():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        inc = models.Incident(key="INC-0001", severity=enums.IncidentSeverity.SEV1,
                              title="service down", description="x", detected_by="watchdog",
                              started_at=notify.datetime.now(notify.timezone.utc))
        db.add(inc)
        db.commit()
        assert svc.dispatch(db, _db_event(db, "SERVICE_DOWN", incident_id=inc.id))[0] is True
        inc.status = enums.IncidentStatus.RESOLVED
        db.commit()
        recovered = _db_event(db, "INCIDENT_RESOLVED", enums.Severity.INFO, inc.id)
        state = db.scalar(select(models.NotificationState))
        assert svc.dispatch(db, recovered)[0] is True
        assert state.active is False
        next_inc = models.Incident(key="INC-0002", severity=enums.IncidentSeverity.SEV1,
                                   title="service down again", description="x", detected_by="watchdog",
                                   started_at=notify.datetime.now(notify.timezone.utc))
        db.add(next_inc)
        db.commit()
        assert svc.dispatch(db, _db_event(db, "SERVICE_DOWN", incident_id=next_inc.id))[0] is True
        assert len(adapter.sent) == 3


def test_action_required_remains_in_desk_without_telegram():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        ev = _db_event(db, "TASK_FAILED_REPEATEDLY", enums.Severity.MEDIUM)
        assert svc.dispatch(db, ev) == (False, "action required in Desk")
        assert not adapter.sent and ev.notify_reason == "action required in Desk"


def test_watchdog_backup_condition_can_recover_and_page_next_occurrence():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        first = _db_event(db, "BACKUP_FAILED", enums.Severity.CRITICAL,
                          condition_key="backup-stale")
        assert svc.dispatch(db, first)[0] is True
        assert svc.dispatch(db, _db_event(db, "BACKUP_FAILED", enums.Severity.CRITICAL,
                                            condition_key="backup-stale"))[0] is False
        assert svc.recover_condition(db, source="watchdog", condition_key="backup-stale") is True
        assert svc.dispatch(db, _db_event(db, "BACKUP_FAILED", enums.Severity.CRITICAL,
                                            condition_key="backup-stale"))[0] is True
        assert len(adapter.sent) == 3


def test_watchdog_episode_recovery_closes_its_notification_scope():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = _db_event(db, "WORKER_UNHEALTHY", enums.Severity.HIGH,
                             condition_key="worker-heartbeat")
        original.payload = {**original.payload,
                            "condition_scope": "watchdog-condition:worker-heartbeat",
                            "condition_episode": 1}
        db.commit()
        assert svc.dispatch(db, original)[0] is True
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "resolved_event_id": original.id, "episode": 1},
        )
        db.add(recovery)
        db.commit()
        assert svc.dispatch(db, recovery)[0] is True
        state = db.scalar(select(models.NotificationState))
        assert state.active is False and state.delivery_status == "recovered"
        assert len(adapter.sent) == 2


def test_recovery_waits_for_in_flight_alert_before_closing_condition():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)

    class InterleavingAdapter(_Adapter):
        recovery = None
        recovery_result = None

        def send(self, text):
            if self.recovery is not None and self.recovery_result is None:
                self.recovery_result = svc.dispatch(db, self.recovery)
            self.sent.append(text)

    adapter = InterleavingAdapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:in-flight",
                     "condition_episode": 1},
        )
        db.add(original)
        db.commit()
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:in-flight",
                     "resolved_event_id": original.id, "episode": 1},
            notify_reason="recovery dispatch pending",
        )
        db.add(recovery)
        db.commit()
        adapter.recovery = recovery

        assert svc.dispatch(db, original) == (True, None)
        assert adapter.recovery_result == (
            False, "alert delivery pending; recovery remains retryable")
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == svc.scope(original)))
        assert state.active and state.delivery_status == "sent"
        assert recovery.notify_reason == "recovery dispatch pending"

        assert svc.dispatch(db, recovery) == (True, None)
        assert recovery.notify_reason == "recovery delivered"
        assert not state.active and state.delivery_status == "recovered"
        assert len(adapter.sent) == 2


def test_uncertain_in_flight_alert_latches_recovery_for_founder_review():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)

    class FailsOriginalAfterInterleave(_Adapter):
        recovery = None
        recovery_result = None

        def send(self, text):
            if self.recovery is not None and self.recovery_result is None:
                self.recovery_result = svc.dispatch(db, self.recovery)
                raise RuntimeError("alert transport outcome uncertain")
            self.sent.append(text)

    adapter = FailsOriginalAfterInterleave()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:uncertain-in-flight",
                     "condition_episode": 1},
        )
        db.add(original)
        db.commit()
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:uncertain-in-flight",
                     "resolved_event_id": original.id, "episode": 1},
            notify_reason="recovery dispatch pending",
        )
        db.add(recovery)
        db.commit()
        adapter.recovery = recovery

        assert svc.dispatch(db, original)[0] is False
        assert adapter.recovery_result == (
            False, "alert delivery pending; recovery remains retryable")
        assert svc.dispatch(db, recovery) == (
            False, "recovery blocked by uncertain alert delivery")
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == svc.scope(original)))
        assert state.active and state.delivery_status == "uncertain"
        assert recovery.notify_reason == "recovery blocked by uncertain alert delivery"
        assert not recovery.notified and len(adapter.sent) == 0


def test_pre_send_recovery_failure_stays_safely_retryable(monkeypatch):
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    svc = notify.NotificationService(_Adapter())
    with Session(engine) as db:
        event = _db_event(db, "INCIDENT_RESOLVED", enums.Severity.INFO)
        monkeypatch.setattr(svc, "_dispatch", lambda *_: (_ for _ in ()).throw(
            RuntimeError("transient before send intent")))

        delivered, reason = svc.dispatch(db, event)

        assert delivered is False and "transient before send intent" in reason
        assert event.notify_reason == "recovery dispatch pending"
        assert not event.notified


def test_dispatch_never_raises_for_detached_expired_event_identity(monkeypatch):
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    svc = notify.NotificationService(_Adapter())
    with Session(engine) as db:
        event = _db_event(db, "WATCHDOG_CONDITION_RECOVERED", enums.Severity.INFO)
        event.notify_reason = "recovery dispatch pending"
        db.commit()
        db.expire(event)
        db.expunge(event)
        monkeypatch.setattr(svc, "_dispatch", lambda *_: (_ for _ in ()).throw(
            RuntimeError("simulated dispatch failure")))

        assert svc.dispatch(db, event) == (False, "event identity unavailable")


def test_dispatch_preserves_recovery_marker_when_rollback_and_marker_read_fail(monkeypatch):
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    svc = notify.NotificationService(_Adapter())
    with Session(engine) as db:
        event = _db_event(db, "WATCHDOG_CONDITION_RECOVERED", enums.Severity.INFO)
        event.notify_reason = "recovery dispatch pending"
        db.commit()
        event_id = event.id
        monkeypatch.setattr(svc, "_dispatch", lambda *_: (_ for _ in ()).throw(
            RuntimeError("simulated dispatch failure")))
        monkeypatch.setattr(db, "rollback", lambda: (_ for _ in ()).throw(
            RuntimeError("database connection lost")))
        monkeypatch.setattr(notify, "Session", lambda **_: (_ for _ in ()).throw(
            RuntimeError("marker lookup unavailable")))

        delivered, reason = svc.dispatch(db, event)
        assert delivered is False and "simulated dispatch failure" in reason

    with Session(engine) as verify:
        saved = verify.get(models.Event, event_id)
        assert saved.notify_reason == "recovery dispatch pending"


def test_new_watchdog_episode_is_not_suppressed_by_delayed_recovery():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                     "condition_episode": 1},
        )
        db.add(original)
        db.commit()
        assert svc.dispatch(db, original)[0] is True

        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                     "resolved_event_id": original.id, "episode": 1},
        )
        renewed = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                     "condition_episode": 2},
        )
        db.add_all([recovery, renewed])
        db.commit()

        assert svc.dispatch(db, renewed)[0] is True
        delivered, reason = svc.dispatch(db, recovery)
        state = db.scalar(select(models.NotificationState))
        assert (delivered, reason) == (False, "stale recovery does not match the active condition episode")
        assert state.active and state.episode == 2 and state.event_id == renewed.id
        assert len(adapter.sent) == 2


def test_delayed_older_watchdog_episode_cannot_block_current_recovery():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    scope = "watchdog-condition:disk"
    with Session(engine) as db:
        current = models.Event(
            event_type="DISK_CRITICAL", source="watchdog", severity=enums.Severity.CRITICAL,
            payload={"condition_key": "disk-critical", "condition_scope": scope,
                     "condition_episode": 2},
        )
        db.add(current)
        db.commit()
        assert svc.dispatch(db, current)[0] is True

        delayed = models.Event(
            event_type="DISK_CRITICAL", source="watchdog", severity=enums.Severity.CRITICAL,
            payload={"condition_key": "disk-critical", "condition_scope": scope,
                     "condition_episode": 1},
        )
        db.add(delayed)
        db.commit()
        assert svc.dispatch(db, delayed) == (False, "stale watchdog condition episode")

        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "disk-critical", "condition_scope": scope,
                     "resolved_event_id": current.id, "episode": 2},
        )
        db.add(recovery)
        db.commit()
        assert svc.dispatch(db, recovery)[0] is True
        state = db.scalar(select(models.NotificationState))
        assert state.active is False and state.episode == 2 and state.event_id == current.id
        assert len(adapter.sent) == 2


def test_repeated_recovery_dispatch_preserves_pending_decision_and_sends_once():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    scope = "watchdog-condition:worker"
    recovery = None

    class ReentrantAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.reentered = False

        def send(self, text):
            if text.startswith("[INFO] WATCHDOG_CONDITION_RECOVERED") and not self.reentered:
                self.reentered = True
                delivered, reason = svc.dispatch(db, recovery)
                assert delivered is False
                assert reason == "recovery delivery pending"
                assert recovery.notify_reason == "recovery delivery pending"
            self.sent.append(text)

    with Session(engine) as db:
        adapter = ReentrantAdapter()
        svc = notify.NotificationService(adapter)
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                     "condition_episode": 1},
        )
        db.add(original)
        db.commit()
        assert svc.dispatch(db, original)[0] is True
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                     "resolved_event_id": original.id, "episode": 1},
        )
        db.add(recovery)
        db.commit()
        assert svc.dispatch(db, recovery)[0] is True
        assert recovery.notify_reason == "recovery delivered"
        assert svc.dispatch(db, recovery) == (True, "recovery delivered")
        assert len(adapter.sent) == 2


def test_stale_recovery_instance_observes_committed_delivery_before_dispatch():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    scope = "watchdog-condition:stale-recovery"
    with Session(engine) as setup:
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                     "condition_episode": 1},
        )
        setup.add(original)
        setup.commit()
        assert svc.dispatch(setup, original) == (True, None)
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                     "resolved_event_id": original.id, "episode": 1},
            notify_reason="recovery dispatch pending",
        )
        setup.add(recovery)
        setup.commit()
        recovery_id = recovery.id

    with Session(engine) as first, Session(engine) as waiting:
        stale = waiting.get(models.Event, recovery_id)
        assert stale.notify_reason == "recovery dispatch pending"
        assert svc.dispatch(first, first.get(models.Event, recovery_id)) == (True, None)
        assert svc.dispatch(waiting, stale) == (True, "recovery delivered")
        assert stale.notify_reason == "recovery delivered"
        assert stale.notified is True
    assert len(adapter.sent) == 2


def test_recovery_then_new_watchdog_episode_delivers_both_notifications():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                     "condition_episode": 1},
        )
        db.add(original)
        db.commit()
        assert svc.dispatch(db, original)[0] is True
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                     "resolved_event_id": original.id, "episode": 1},
        )
        db.add(recovery)
        db.commit()
        assert svc.dispatch(db, recovery)[0] is True

        renewed = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                     "condition_episode": 2},
        )
        db.add(renewed)
        db.commit()
        assert svc.dispatch(db, renewed)[0] is True
        state = db.scalar(select(models.NotificationState))
        assert state.active and state.episode == 2 and state.event_id == renewed.id
        assert len(adapter.sent) == 3


def test_slow_old_send_cannot_overwrite_new_watchdog_episode():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    renewed = models.Event(
        event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
        payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                 "condition_episode": 2},
    )
    db.add(renewed)
    db.flush()

    class InterleavingAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.interleaved = False

        def send(self, text):
            if not self.interleaved:
                self.interleaved = True
                assert svc.dispatch(db, renewed)[0] is True
            self.sent.append(text)

    adapter = InterleavingAdapter()
    svc = notify.NotificationService(adapter)
    original = models.Event(
        event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
        payload={"condition_key": "worker-heartbeat", "condition_scope": "watchdog-condition:mem",
                 "condition_episode": 1},
    )
    db.add(original)
    db.commit()

    try:
        assert svc.dispatch(db, original)[0] is True
        state = db.scalar(select(models.NotificationState))
        assert state.active and state.episode == 2 and state.event_id == renewed.id
        assert state.delivery_status == "sent" and len(adapter.sent) == 2
        assert original.notified is True
    finally:
        db.close()


def test_recovery_send_failure_is_persisted_as_ambiguous_not_resendable():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)

    class FailsRecovery(_Adapter):
        def send(self, text):
            self.sent.append(text)
            if len(self.sent) == 2:
                raise RuntimeError("transport ended after send attempt")

    adapter = FailsRecovery()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = _db_event(db, "WORKER_UNHEALTHY", enums.Severity.HIGH,
                             condition_key="worker-heartbeat")
        original.payload = {**original.payload,
                            "condition_scope": "watchdog-condition:worker-heartbeat",
                            "condition_episode": 1}
        db.commit()
        assert svc.dispatch(db, original)[0] is True
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "resolved_event_id": original.id, "episode": 1},
        )
        db.add(recovery)
        db.commit()
        assert svc.dispatch(db, recovery)[0] is False
        state = db.scalar(select(models.NotificationState))
        assert state.active and state.delivery_status == "uncertain"
        assert recovery.notify_reason == "recovery delivery failed or uncertain"
        assert len(adapter.sent) == 2

        renewed = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "condition_episode": 2},
        )
        db.add(renewed)
        db.commit()
        assert svc.dispatch(db, renewed) == (
            False, "prior condition delivery uncertain; manual review required")
        assert renewed.notify_reason == "prior condition delivery uncertain; manual review required"
        assert len(adapter.sent) == 2


def test_new_episode_does_not_overtake_crash_pending_recovery():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "condition_episode": 1},
        )
        db.add(original)
        db.commit()
        assert svc.dispatch(db, original)[0] is True
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "resolved_event_id": original.id, "episode": 1},
            notify_reason="recovery delivery pending",
        )
        db.add(recovery)
        db.commit()

        renewed = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "condition_episode": 2},
        )
        db.add(renewed)
        db.commit()
        assert svc.dispatch(db, renewed) == (
            False, "prior recovery delivery uncertain; manual review required")
        state = db.scalar(select(models.NotificationState))
        assert state.active and state.episode == 1 and state.delivery_status == "uncertain"
        assert len(adapter.sent) == 1


def test_post_send_commit_failure_preserves_ambiguous_recovery_marker(monkeypatch):
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)

    class RecordingAdapter(_Adapter):
        pass

    adapter = RecordingAdapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        original = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "condition_episode": 1},
        )
        db.add(original)
        db.commit()
        assert svc.dispatch(db, original)[0] is True
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "resolved_event_id": original.id, "episode": 1},
        )
        db.add(recovery)
        db.commit()

        real_commit = db.commit

        def fail_after_recovery_send():
            if (len(adapter.sent) == 2 and recovery.notify_reason == "recovery delivered"):
                raise RuntimeError("simulated database commit loss after Telegram accepted")
            real_commit()

        monkeypatch.setattr(db, "commit", fail_after_recovery_send)
        delivered, error = svc.dispatch(db, recovery)
        assert delivered is False and "simulated database commit loss" in error
        assert recovery.notify_reason == "recovery delivery pending"
        assert len(adapter.sent) == 2
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == svc.scope(original)))
        assert state.active and state.event_id == original.id and state.episode == 1
        assert svc._pending_watchdog_recovery(db, original.id, 1) == "recovery delivery pending"

        renewed = models.Event(
            event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
            payload={"condition_key": "worker-heartbeat",
                     "condition_scope": "watchdog-condition:worker-heartbeat",
                     "condition_episode": 2},
        )
        db.add(renewed)
        real_commit()
        assert svc.dispatch(db, renewed) == (
            False, "prior recovery delivery uncertain; manual review required")
        assert renewed.notify_reason == "prior recovery delivery uncertain; manual review required"
        assert len(adapter.sent) == 2


def test_failure_is_uncertain_and_not_automatically_resent(token):
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    class Failing(_Adapter):
        def send(self, text):
            raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    svc = notify.NotificationService(Failing())
    with Session(engine) as db:
        first = _db_event(db)
        assert svc.dispatch(db, first)[0] is False
        assert TOKEN not in first.notify_error
        second = _db_event(db)
        assert svc.dispatch(db, second)[0] is False
        assert second.notify_reason.startswith("delivery pending or uncertain")


def test_uncertain_delivery_requires_explicit_delayed_retry():
    from datetime import datetime, timedelta, timezone

    class Flaky(_Adapter):
        fail = True

        def send(self, text):
            if self.fail:
                raise RuntimeError("transport failed")
            super().send(text)

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = Flaky()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        event = _db_event(db)
        assert svc.dispatch(db, event)[0] is False
        with pytest.raises(ValueError, match="in flight"):
            svc.retry_uncertain(db, event)
        state = db.scalar(select(models.NotificationState))
        state.attempted_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        db.commit()
        adapter.fail = False
        assert svc.retry_uncertain(db, event)[0] is True
        assert len(adapter.sent) == 1


def test_founder_can_resume_exhausted_recovery_after_source_alert_is_sent():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        incident = models.Incident(
            key="INC-RETRY", severity=enums.IncidentSeverity.SEV1,
            title="service recovered", description="test", detected_by="test",
            started_at=notify.datetime.now(notify.timezone.utc),
            status=enums.IncidentStatus.RESOLVED,
            resolved_at=notify.datetime.now(notify.timezone.utc),
        )
        db.add(incident)
        db.commit()
        original = _db_event(db, "SERVICE_DOWN", enums.Severity.HIGH, incident.id)
        assert svc.dispatch(db, original) == (True, None)
        recovery = _db_event(db, "INCIDENT_RESOLVED", enums.Severity.INFO, incident.id)
        recovery.notify_reason = "recovery dispatch retries exhausted; founder review required"
        db.commit()

        with pytest.raises(ValueError, match="no uncertain delivery"):
            svc.retry_uncertain(db, original)
        assert svc.retry_uncertain(db, recovery) == (True, None)
        assert recovery.notify_reason == "recovery delivered"
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == svc.scope(recovery)))
        assert not state.active and state.delivery_status == "recovered"
        assert len(adapter.sent) == 2


def test_missing_token_records_configuration_failure(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        ev = _db_event(db)
        assert notify.NotificationService().dispatch(db, ev)[0] is False
        assert ev.notify_error == "Telegram adapter not configured"


def test_partial_incident_update_is_not_a_recovery(monkeypatch):
    from starlette.requests import Request
    from app import approval_identity, main, schemas

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    monkeypatch.setattr(main, "notifier", svc)
    with Session(engine) as db:
        inc = models.Incident(key="INC-0001", severity=enums.IncidentSeverity.SEV1,
                              title="service down", description="x", detected_by="watchdog",
                              started_at=notify.datetime.now(notify.timezone.utc))
        db.add(inc)
        db.commit()
        assert svc.dispatch(db, _db_event(db, incident_id=inc.id))[0] is True
        request = Request({"type": "http", "method": "POST", "path": "/incidents/INC-0001/resolve",
                           "headers": []})
        request.state.principal = approval_identity.founder("founder@example.invalid")
        body = schemas.IncidentResolve(root_cause="temporary outage", resolution="service still degraded",
                                       status=enums.IncidentStatus.MITIGATED)
        main.resolve_incident(inc.key, body, db, request)
        state = db.scalar(select(models.NotificationState))
        assert state.active is True
        assert len(adapter.sent) == 1
        assert db.scalars(select(models.Event).order_by(models.Event.id.desc())).first().event_type == "INCIDENT_UPDATED"


def test_forged_recovery_event_cannot_close_an_open_incident():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    adapter = _Adapter()
    svc = notify.NotificationService(adapter)
    with Session(engine) as db:
        inc = models.Incident(key="INC-0001", severity=enums.IncidentSeverity.SEV1,
                              title="still down", description="x", detected_by="watchdog",
                              started_at=notify.datetime.now(notify.timezone.utc))
        db.add(inc)
        db.commit()
        assert svc.dispatch(db, _db_event(db, incident_id=inc.id))[0] is True
        forged = _db_event(db, "INCIDENT_RESOLVED", enums.Severity.INFO, inc.id)
        assert svc.dispatch(db, forged) == (False, "incident not resolved")
        assert db.scalar(select(models.NotificationState)).active is True
        injected_condition = _db_event(db, "CONDITION_RECOVERED", enums.Severity.INFO,
                                       condition_key="worker-heartbeat")
        assert svc.dispatch(db, injected_condition) == (False, "informational only")
        assert len(adapter.sent) == 1


# --- the bot token must never reach the logs (httpx logs request URLs at INFO) -------------------------------
def test_a_send_never_writes_the_bot_token_to_the_logs(monkeypatch, caplog):
    import logging

    import httpx

    from app import notify as notify_module
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefg"
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", token)
    monkeypatch.setattr(notify_module.settings, "telegram_admin_chat_ids", "42")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))

    def post(url, **kw):
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kw)
    monkeypatch.setattr(notify_module.httpx, "post", post)
    for name in ("httpx", "httpcore"):             # as if nothing had configured them yet
        logging.getLogger(name).setLevel(logging.NOTSET)
    notify_module.silence_http_logs()
    with caplog.at_level(logging.INFO):             # the production root level
        notify_module.TelegramAdapter().send("hello")
    assert token not in caplog.text and "/bot" not in caplog.text
    assert not any(r.name in ("httpx", "httpcore") and r.levelno < logging.WARNING for r in caplog.records)


def test_every_process_that_can_send_silences_http_info_logs():
    import logging

    import app.main  # noqa: F401 - the API process
    import app.watchdog  # noqa: F401 - the watchdog process
    import app.worker  # noqa: F401 - the worker process
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_the_token_filter_does_not_change_delivery(monkeypatch):
    from app import notify as notify_module
    sent = []
    monkeypatch.setattr(notify_module.httpx, "post", lambda url, **kw: sent.append((url, kw)) or type(
        "R", (), {"status_code": 200, "json": lambda self: {"ok": True}})())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefg")
    monkeypatch.setattr(notify_module.settings, "telegram_admin_chat_ids", "42,43")
    notify_module.TelegramAdapter().send("x")
    assert len(sent) == 2 and all(u.endswith("/sendMessage") for u, _ in sent)
