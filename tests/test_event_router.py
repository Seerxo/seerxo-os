"""Event -> incident -> task routing is deterministic and never duplicates work."""
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import enums, event_router, models, notify, watchdog


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    unit = models.BusinessUnit(key="youtube", name="YouTube", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P0)
    db.add_all([unit, models.Agent(key="developer", role="dev", enabled=True)])
    db.flush()
    db.add(models.Service(key="youtube-auto", name="YouTube", status=enums.ServiceStatus.ACTIVE,
                          risk=enums.RiskLevel.HIGH, business_unit_id=unit.id))
    db.commit()
    return db


def _count(db, model):
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def _missing_publish(db):
    from datetime import datetime, timezone
    return watchdog.emit(db, [watchdog.Finding(
        "SCHEDULED_PRODUCTION_MISSING", enums.Severity.HIGH, "@AxiomTalesHQ: no publish for 91h",
        "threshold 48h", service_key="youtube-auto", dedupe="youtube-inactive")],
        facts={"collected_at": datetime.now(timezone.utc).isoformat()})


def test_stale_production_opens_one_incident_and_one_developer_task():
    db = _db()
    _missing_publish(db)
    task = db.execute(select(models.Task)).scalar_one()
    inc = db.execute(select(models.Incident)).scalar_one()
    assert task.status == enums.TaskStatus.READY and task.priority == enums.Priority.P0
    assert db.get(models.Agent, task.assigned_agent_id).key == "developer"
    assert task.context == {"incident": inc.key, "source_event": 1, "autonomous": False}
    assert inc.severity == enums.IncidentSeverity.SEV2 and inc.service_id is not None
    assert task.business_unit_id is not None
    # Break caught: the watchdog fires every 10 minutes; each tick must not open work.
    _missing_publish(db)
    _missing_publish(db)
    assert _count(db, models.Incident) == 1 and _count(db, models.Task) == 1


def test_an_incident_a_human_already_opened_keeps_its_owner():
    db = _db()
    svc = db.execute(select(models.Service)).scalar_one()
    db.add(models.Incident(key="INC-0001", severity=enums.IncidentSeverity.SEV1, title="manual",
                           description="x", started_at=svc.created_at, detected_by="founder",
                           service_id=svc.id))
    db.commit()
    _missing_publish(db)
    assert _count(db, models.Incident) == 1 and _count(db, models.Task) == 0


def test_resolved_incident_allows_a_new_one():
    db = _db()
    _missing_publish(db)
    db.execute(select(models.Incident)).scalar_one().status = enums.IncidentStatus.RESOLVED
    db.commit()
    # The stale finding alone is not a new episode; replaying it must not recreate work.
    _missing_publish(db)
    assert _count(db, models.Incident) == 1 and _count(db, models.Task) == 1

    # A fresh publish proves recovery. A later missed publish is a genuine recurrence.
    from datetime import datetime, timezone
    watchdog.emit(db, [], facts={"youtube_last_publish_age_h": 10,
                                 "collected_at": datetime.now(timezone.utc).isoformat()})
    _missing_publish(db)
    assert _count(db, models.Incident) == 2


def test_unrouted_and_unscoped_events_do_nothing():
    db = _db()
    watchdog.emit(db, [watchdog.Finding("WORKER_UNHEALTHY", enums.Severity.HIGH, "x", "y"),
                       watchdog.Finding("SERVICE_DOWN", enums.Severity.HIGH, "no service", "y")])
    assert _count(db, models.Incident) == 0 and _count(db, models.Task) == 0


def test_routing_failure_never_loses_the_event(monkeypatch):
    db = _db()
    monkeypatch.setattr(event_router, "route", lambda db, ev: 1 / 0)
    _missing_publish(db)
    assert _count(db, models.Event) == 1 and _count(db, models.Incident) == 0


def test_routed_production_task_is_never_opted_into_the_worker():
    from app import registry, worker
    db = _db()
    _missing_publish(db)
    out = worker.run_once(db, {}, registry.load())
    assert out["model_calls"] == 0 and out["work"] is None


def test_routed_alert_and_original_event_share_one_notification(monkeypatch):
    class Adapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    adapter = Adapter()
    service = notify.NotificationService(adapter)
    monkeypatch.setattr(event_router, "notifier", service)
    monkeypatch.setattr(watchdog, "notifier", service)
    db = _db()
    watchdog.emit(db, [watchdog.Finding(
        "YOUTUBE_PIPELINE_STOPPED", enums.Severity.HIGH, "pipeline stopped", "unavailable",
        service_key="youtube-auto", dedupe="pipeline-stopped")])
    watchdog.emit(db, [watchdog.Finding(
        "PRODUCTION_AUTOMATION_STOPPED", enums.Severity.HIGH, "same outage", "still down",
        service_key="youtube-auto", dedupe="automation-stopped")])
    assert len(adapter.sent) == 1
    rows = db.scalars(select(models.Event).order_by(models.Event.id)).all()
    routed = [event for event in rows if event.incident_id is not None]
    assert len({event.incident_id for event in routed}) == 1
    assert rows[1].notified is True
    assert rows[-1].notify_reason == "active incident already notified"


def test_repaired_watchdog_route_keeps_event_append_only_and_suppresses_duplicate_alert(monkeypatch):
    class Adapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    adapter = Adapter()
    service = notify.NotificationService(adapter)
    monkeypatch.setattr(event_router, "notifier", service)
    db = _db()
    svc = db.scalar(select(models.Service).where(models.Service.key == "youtube-auto"))
    event = models.Event(
        event_type="YOUTUBE_PIPELINE_STOPPED", source="watchdog", severity=enums.Severity.HIGH,
        payload={"title": "pipeline stopped", "detail": "no output", "condition_key": "pipeline",
                 "condition_scope": "watchdog-condition:episode-1", "condition_episode": 1,
                 "service_key": "youtube-auto"},
    )
    db.add(event)
    db.flush()
    original_payload = dict(event.payload)
    db.add(models.NotificationState(scope_key="watchdog-condition:episode-1", active=True,
                                     episode=1, event_id=event.id, delivery_status="processing"))
    db.add(models.NotificationState(scope_key=service.scope(event), active=True, episode=1,
                                     severity="HIGH", event_id=event.id, delivery_status="sent"))
    db.commit()

    routed = event_router.route(db, event)

    assert routed and routed["created"] is True
    assert event.service_id is None and event.payload == original_payload
    assert db.scalar(select(models.Incident)).service_id == svc.id
    alert = db.scalar(select(models.Event).where(models.Event.event_type == "INCIDENT_CREATED"))
    assert alert.notify_reason == "source watchdog alert already sent or delivery is ambiguous"
    incident_state = db.get(models.NotificationState, f"incident:{event.incident_id}")
    assert incident_state.delivery_status == "sent" and incident_state.event_id == event.id
    assert adapter.sent == []


def test_route_cooldown_redrive_reuses_existing_alert_delivery(monkeypatch):
    from datetime import datetime, timedelta, timezone

    class Adapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    adapter = Adapter()
    service = notify.NotificationService(adapter)
    monkeypatch.setattr(event_router, "notifier", service)
    monkeypatch.setattr(watchdog, "notifier", service)
    original_route = event_router.route
    route_calls = []

    def fail_three_then_recover(db, event):
        route_calls.append(event.id)
        if len(route_calls) <= 3:
            raise RuntimeError("temporary route outage")
        return original_route(db, event)

    monkeypatch.setattr(event_router, "route", fail_three_then_recover)
    db = _db()
    finding = watchdog.Finding("YOUTUBE_PIPELINE_STOPPED", enums.Severity.HIGH,
                               "pipeline stopped", "no output",
                               service_key="youtube-auto", dedupe="pipeline-stopped-retry")
    now = datetime.now(timezone.utc)

    watchdog.emit(db, [finding], {"collected_at": now.isoformat()})
    for minute in (6, 12):
        scope = watchdog._watchdog_condition_scope(finding)
        state = db.get(models.NotificationState, scope)
        state.attempted_at = now - timedelta(minutes=minute)
        db.commit()
        watchdog.emit(db, [finding], {"collected_at": (now + timedelta(minutes=minute)).isoformat()})

    scope = watchdog._watchdog_condition_scope(finding)
    state = db.get(models.NotificationState, scope)
    assert state.delivery_status == "route_failed" and len(adapter.sent) == 1
    source_event = db.scalar(select(models.Event).where(
        models.Event.source == "watchdog", models.Event.event_type == "YOUTUBE_PIPELINE_STOPPED"))
    original_service_id = source_event.service_id
    state.attempted_at = now - timedelta(minutes=31)
    db.commit()
    watchdog.emit(db, [finding], {"collected_at": (now + timedelta(minutes=13)).isoformat()})

    source_event = db.get(models.Event, source_event.id)
    incident = db.scalar(select(models.Incident))
    incident_state = db.get(models.NotificationState, f"incident:{incident.id}")
    assert incident is not None and incident_state.delivery_status == "sent"
    assert incident_state.event_id == source_event.id and len(adapter.sent) == 1
    assert source_event.service_id == original_service_id
    assert source_event.payload["service_key"] == "youtube-auto"


def test_missing_scheduled_publish_creates_desk_work_without_paging(monkeypatch):
    class Adapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    adapter = Adapter()
    service = notify.NotificationService(adapter)
    monkeypatch.setattr(event_router, "notifier", service)
    monkeypatch.setattr(watchdog, "notifier", service)
    db = _db()
    _missing_publish(db)
    assert db.scalar(select(models.Incident)).severity == enums.IncidentSeverity.SEV2
    assert db.scalar(select(models.Task)).status == enums.TaskStatus.READY
    assert adapter.sent == []


def test_confirmed_outage_escalates_existing_scheduled_incident(monkeypatch):
    class Adapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    adapter = Adapter()
    service = notify.NotificationService(adapter)
    monkeypatch.setattr(event_router, "notifier", service)
    monkeypatch.setattr(watchdog, "notifier", service)
    db = _db()
    _missing_publish(db)
    inc = db.scalar(select(models.Incident))
    assert inc.severity == enums.IncidentSeverity.SEV2 and not adapter.sent
    watchdog.emit(db, [watchdog.Finding(
        "YOUTUBE_PIPELINE_STOPPED", enums.Severity.HIGH, "confirmed stop", "not running",
        service_key="youtube-auto", dedupe="pipeline-stopped")])
    db.refresh(inc)
    assert inc.severity == enums.IncidentSeverity.SEV1
    assert len(adapter.sent) == 1
