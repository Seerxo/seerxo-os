"""Watchdog threshold and container-resolution tests.

These run without a database: every function under test is pure.
"""
from datetime import datetime, timedelta, timezone

from app import enums
from app.watchdog import (
    _resolve_container, check_host, check_http, check_metric_sources, check_production_activity,
    check_stage_c,
)


def test_healthy_host_reports_nothing():
    assert check_host({"disk_used_pct": 29, "mem_used_pct": 45}) == []


def test_disk_high_and_critical_are_distinguished():
    high = check_host({"disk_used_pct": 88})
    assert len(high) == 1 and high[0].severity is enums.Severity.HIGH

    crit = check_host({"disk_used_pct": 96})
    assert len(crit) == 1 and crit[0].severity is enums.Severity.CRITICAL


def test_disk_boundary_is_inclusive():
    assert check_host({"disk_used_pct": 85})[0].severity is enums.Severity.HIGH
    assert check_host({"disk_used_pct": 84}) == []


def test_missing_facts_do_not_raise():
    # A collector that failed must not take the watchdog down with it.
    assert check_host({}) == []
    assert check_host({"disk_used_pct": None}) == []


def test_missing_container_snapshot_is_unknown_not_all_services_down():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models
    from app.watchdog import check_containers

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Service(key="api", name="API", container="company-api",
                              status=enums.ServiceStatus.ACTIVE, risk=enums.RiskLevel.LOW))
        db.commit()
        assert check_containers(db, {}) == []
        assert check_containers(db, {"containers": {}, "containers_complete": False}) == []
        running_but_health_unknown = {
            "containers": {"company-api": "running"},
            "container_health": {"company-api": "unhealthy"},
            "container_health_complete": False,
        }
        assert check_containers(db, running_but_health_unknown) == []
        running_and_unhealthy = {**running_but_health_unknown, "container_health_complete": True}
        assert check_containers(db, running_and_unhealthy)[0].dedupe == "unhealthy:api"


def test_memory_pressure_flagged_because_host_has_no_swap():
    out = check_host({"mem_used_pct": 95})
    assert out and out[0].event_type == "WORKER_UNHEALTHY"


def test_http_200_is_silent_and_502_alerts():
    assert check_http({"http": {"s": {"code": 200, "url": "u"}}}) == []
    bad = check_http({"http": {"s": {"code": 502, "url": "u", "service": "seerxo-api"}}})
    assert len(bad) == 1
    assert bad[0].event_type == "SERVICE_DOWN"
    assert bad[0].service_key == "seerxo-api"


def test_coolify_deployment_suffix_prefix_matches():
    # Coolify renames the container on every deploy; pinning the full name would make the
    # watchdog cry wolf after each deployment.
    states = {"company-api": "running", "wr2sdv20joia2t3r0f6j60ow-023625132569": "running"}
    assert _resolve_container("company-api", states) == "company-api"
    assert _resolve_container("wr2sdv20joia2t3r0f6j60ow", states) == \
        "wr2sdv20joia2t3r0f6j60ow-023625132569"


def test_unknown_container_does_not_resolve():
    assert _resolve_container("nope", {"x": "running"}) is None


def test_ambiguous_prefix_is_not_guessed():
    # Two candidates means a stale container was left behind; guessing would hide it.
    assert _resolve_container("x", {"x-1": "running", "x-2": "exited"}) is None


def test_production_inactivity_threshold():
    assert check_production_activity(None, {"youtube_last_publish_age_h": 10}) == []
    out = check_production_activity(None, {"youtube_last_publish_age_h": 72})
    assert out[0].event_type == "SCHEDULED_PRODUCTION_MISSING"
    assert out[0].service_key == "youtube-auto"


# --- heartbeats ------------------------------------------------------------------

def test_idle_agent_does_not_raise_a_worker_alert(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Agent(key="research", role="research", enabled=True,
                            last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=2)))
        db.commit()
    monkeypatch.setattr(watchdog, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(watchdog, "check_backups", lambda db: [])
    monkeypatch.setattr(watchdog, "_newest_metric_by_source", lambda db: {})
    monkeypatch.setattr(watchdog, "record_stage_c_readiness", lambda db, facts: None)
    monkeypatch.setattr(watchdog, "record_host_metrics", lambda db, facts: None)
    monkeypatch.setattr(watchdog.reconciler_heartbeat, "run", lambda db, fn: None)
    monkeypatch.setattr(watchdog, "run_experiment_scheduler", lambda: None)
    monkeypatch.setattr(watchdog.notifier, "recover_condition", lambda *args, **kwargs: None)
    monkeypatch.setattr(watchdog, "emit", lambda db, findings, facts=None: {"findings": findings})

    assert watchdog.run({})["detail"] == []


def test_host_metrics_overwrite_one_row_and_survive_a_failed_collector():
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models
    from app.watchdog import HOST_METRICS_KEY, record_host_metrics

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        record_host_metrics(db, {"cpu_used_pct": 12, "mem_used_pct": 45, "disk_used_pct": 29})
        record_host_metrics(db, {"cpu_used_pct": 30, "mem_used_pct": 50, "disk_used_pct": 31})
        row = db.get(models.ControlState, HOST_METRICS_KEY)
        assert row.value["cpu_percent"] == 30 and row.value["disk_percent"] == 31
        # Break caught: metrics stored as history would grow the table every 10 minutes.
        assert db.execute(select(func.count()).select_from(models.ControlState)).scalar_one() == 1
        # Break caught: a failed collector blanks the Desk instead of leaving an aging reading.
        assert record_host_metrics(db, {"disk_used_pct": None}) is None
        assert db.get(models.ControlState, HOST_METRICS_KEY).value["ram_percent"] == 50


def test_delayed_host_snapshot_cannot_overwrite_newer_metrics():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models
    from app.watchdog import HOST_METRICS_KEY, record_host_metrics

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        newer = {"cpu_used_pct": 70, "mem_used_pct": 71, "disk_used_pct": 72,
                 "collected_at": (now + timedelta(seconds=2)).isoformat()}
        older = {"cpu_used_pct": 10, "mem_used_pct": 11, "disk_used_pct": 12,
                 "collected_at": (now + timedelta(seconds=1)).isoformat()}
        assert record_host_metrics(db, newer) is not None
        assert record_host_metrics(db, older) is None
        row = db.get(models.ControlState, HOST_METRICS_KEY)
        assert row.value["cpu_percent"] == 70
        assert row.value["at"] == newer["collected_at"]


def test_metric_source_past_its_freshness_window_alerts():
    now = datetime.now(timezone.utc)
    fresh = {"seerxo-backend": now - timedelta(hours=2), "youtube-auto": now - timedelta(hours=60)}
    assert check_metric_sources(fresh) == []
    stale = check_metric_sources({"seerxo-backend": now - timedelta(hours=4),
                                  "youtube-auto": now - timedelta(hours=60)})
    assert [f.dedupe for f in stale] == ["metric-stale:seerxo-backend"]
    assert stale[0].event_type == "METRIC_SOURCE_STALE" and "4h" in stale[0].title
    # Stale metrics stay visible in the Desk without interrupting the founder.
    from app.notify import NotificationService
    from app.models import Event
    assert NotificationService().category(Event(event_type=stale[0].event_type,
        source="watchdog", severity=stale[0].severity)) == "informational"


def test_stage_c_drift_alerts_once_only_when_code_writes_are_enabled():
    assert check_stage_c({"stage_c": {"enabled": False, "report": {"ready": False}}}) == []
    assert check_stage_c({"stage_c": {"enabled": True, "report": {"ready": True}}}) == []
    out = check_stage_c({"stage_c": {"enabled": True, "report": {"ready": False,
                                                     "checks": [{"name": "egress_proxy", "status": "FAIL"}]}}})
    assert len(out) == 1
    assert out[0].event_type == "WORKER_UNHEALTHY"
    assert out[0].dedupe == "stage-c-readiness"
    assert "egress_proxy" in out[0].detail
    from app.notify import NotificationService
    from app.models import Event
    assert NotificationService().category(Event(event_type=out[0].event_type,
        source="watchdog", severity=out[0].severity,
        payload={"condition_key": out[0].dedupe})) == "action_required"


def test_unregistered_or_never_reported_sources_are_silent():
    old = datetime.now(timezone.utc) - timedelta(days=30)
    assert check_metric_sources({"some-test-source": old}) == []
    assert check_metric_sources({}) == []


def test_the_live_stage_c_report_is_stored_for_admission_to_enforce():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models
    from app.watchdog import STAGE_C_READINESS_KEY, record_stage_c_readiness
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        report = {"ready": False, "source_sha": "a" * 40,
                  "checks": [{"name": "launcher", "status": "FAIL"}, {"name": "egress_proxy", "status": "PASS"}]}
        # stored whether or not code writes are enabled, so flipping the flag has an immediate answer
        stored = record_stage_c_readiness(db, {"stage_c": {"enabled": False, "report": report}})
        row = db.get(models.ControlState, STAGE_C_READINESS_KEY)
        assert row.updated_by == "watchdog" and stored == row.value
        assert row.value["ready"] is False and row.value["failed"] == ["launcher"]
        assert row.value["source_sha"] == "a" * 40 and row.value["checked_at"]
        record_stage_c_readiness(db, {"stage_c": {"report": {"ready": True, "source_sha": "a" * 40, "checks": []}}})
        assert db.get(models.ControlState, STAGE_C_READINESS_KEY).value["ready"] is True
        # a preflight that could not run must not leave the last READY standing
        for broken in ({"stage_c": {"report": None}}, {"stage_c": {"report": {"ready": "yes", "checks": []}}}, {}):
            value = record_stage_c_readiness(db, broken)
            assert value["ready"] is False and value["failed"] == ["preflight_unavailable"], broken
            assert db.get(models.ControlState, STAGE_C_READINESS_KEY).value["ready"] is False
            record_stage_c_readiness(db, {"stage_c": {"report": {"ready": True, "source_sha": "a" * 40, "checks": []}}})


def test_watchdog_emits_one_event_per_active_condition_episode():
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding(
        "TASK_FAILED_REPEATEDLY", enums.Severity.HIGH,
        "5 task(s) exhausted their retries", "TASK-0062, TASK-0063",
        dedupe="deadletter",
    )

    first_observed = datetime.now(timezone.utc) - timedelta(minutes=2)
    next_observed = first_observed + timedelta(minutes=1)
    with Session(engine) as db:
        first = watchdog.emit(db, [finding, finding], {"collected_at": first_observed.isoformat()})
        repeated = watchdog.emit(db, [finding], {"collected_at": next_observed.isoformat()})
        assert first["emitted"] == 1
        assert first["deduplicated"] == 1
        assert repeated["emitted"] == 0
        assert repeated["deduplicated"] == 1
        assert db.scalar(select(func.count()).select_from(models.Event)) == 1
        condition_event = db.scalar(select(models.Event))
        assert condition_event.payload["last_seen_at"] == first_observed.isoformat()
        # Observation checkpoints live outside append-only event evidence.
        assert condition_event.payload.get("last_seen_at") == first_observed.isoformat()
        checkpoint = db.get(models.ControlState, watchdog._condition_checkpoint_key(
            watchdog._watchdog_condition_scope(finding)))
        assert checkpoint.value["last_seen_at"] == next_observed.isoformat()

        recovered = watchdog.emit(db, [], facts={"collected_at": next_observed + timedelta(minutes=1)})
        assert recovered["recovered"] == 1
        assert db.scalar(select(func.count()).select_from(models.Event)) == 2
        condition = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key.startswith(watchdog.WATCHDOG_CONDITION_PREFIX)))
        events = db.scalars(select(models.Event).order_by(models.Event.id)).all()
        assert not condition.active and condition.episode == 1
        assert events[1].event_type == "WATCHDOG_CONDITION_RECOVERED"
        assert events[1].payload["resolved_event_id"] == events[0].id

        recurrence = watchdog.emit(db, [finding], {
            "collected_at": (next_observed + timedelta(minutes=2)).isoformat()})
        assert recurrence["emitted"] == 1
        assert db.scalar(select(func.count()).select_from(models.Event)) == 3
        assert condition.active and condition.episode == 2


def test_unattempted_watchdog_recovery_notification_resumes_after_restart(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        pending = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "mem-high", "condition_scope": "watchdog-condition:mem"},
            notify_reason="recovery dispatch pending",
        )
        already_attempted = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": "disk-high", "condition_scope": "watchdog-condition:disk"},
            notify_reason="recovery delivery failed or uncertain",
        )
        incident_recovery = models.Event(
            event_type="INCIDENT_RESOLVED", source="company-api",
            severity=enums.Severity.INFO,
            payload={"title": "resolved incident"},
            notify_reason="recovery dispatch pending",
        )
        db.add_all([pending, already_attempted, incident_recovery])
        db.commit()
        delivered = []

        def dispatch(session, event):
            delivered.append(event.id)
            event.notify_reason = "recovery delivered"
            event.notified = True
            session.commit()
            return True, None

        monkeypatch.setattr(watchdog.notifier, "dispatch", dispatch)
        watchdog.emit(db, [], {})
        assert delivered == [pending.id, incident_recovery.id]
        assert pending.notified is True
        assert incident_recovery.notified is True


def test_failed_recovery_dispatch_has_cooldown_and_three_attempt_limit(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    now = [datetime.now(timezone.utc)]
    calls = []
    monkeypatch.setattr(watchdog, "_now", lambda: now[0])

    def fail_before_send(db, event):
        calls.append(event.id)
        event.notify_reason = "recovery dispatch pending"
        event.notify_error = "transient database error"
        db.commit()
        return False, event.notify_error

    monkeypatch.setattr(watchdog.notifier, "dispatch", fail_before_send)
    with Session(engine) as db:
        event = models.Event(event_type="INCIDENT_RESOLVED", source="company-api",
                             severity=enums.Severity.INFO, payload={"title": "resolved"},
                             notify_reason="recovery dispatch pending")
        db.add(event)
        db.commit()
        event_id = event.id

        watchdog.emit(db, [], {})
        assert calls == [event_id]
        watchdog.emit(db, [], {})
        assert calls == [event_id]  # cooldown prevents per-tick retries

        for _ in range(2):
            now[0] += timedelta(minutes=6)
            watchdog.emit(db, [], {})
        assert calls == [event_id, event_id, event_id]
        assert event.notify_reason == "recovery dispatch retries exhausted; founder review required"

        now[0] += timedelta(minutes=6)
        watchdog.emit(db, [], {})
        assert calls == [event_id, event_id, event_id]


def test_missing_source_data_does_not_close_a_watchdog_condition():
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe="disk-critical")
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"disk_used_pct": 96, "collected_at": (now - timedelta(minutes=2)).isoformat()})
        unknown = watchdog.emit(db, [], {})
        assert unknown["recovered"] == 0
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED")) == 0

        no_timestamp = watchdog.emit(db, [], {"disk_used_pct": 80})
        assert no_timestamp["recovered"] == 0
        resolved = watchdog.emit(db, [], {"disk_used_pct": 80, "collected_at": now.isoformat()})
        assert resolved["recovered"] == 1
        recovery = db.scalar(select(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED"))
        assert recovery.payload["resolved_event_id"] is not None

        watchdog.emit(db, [finding], {"disk_used_pct": 96, "collected_at": (now + timedelta(minutes=1)).isoformat()})
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key.startswith(watchdog.WATCHDOG_CONDITION_PREFIX)))
        assert state.active and state.episode == 2


def test_older_watchdog_snapshot_cannot_recover_a_newer_condition():
    from datetime import timedelta
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("WORKER_UNHEALTHY", enums.Severity.HIGH,
                               "Memory at 95%", "Host memory pressure", dedupe="mem-high")
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"mem_used_pct": 95,
                                      "collected_at": (now + timedelta(seconds=2)).isoformat()})
        result = watchdog.emit(db, [], {"mem_used_pct": 45,
                                        "collected_at": (now + timedelta(seconds=1)).isoformat()})
        assert result["recovered"] == 0
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key.startswith(watchdog.WATCHDOG_CONDITION_PREFIX)))
        assert state.active


def test_older_finding_cannot_reopen_a_recovered_watchdog_episode():
    from datetime import timedelta
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("WORKER_UNHEALTHY", enums.Severity.HIGH,
                               "Memory at 95%", "Host memory pressure", dedupe="mem-high")
    now = datetime.now(timezone.utc)
    t1, t2 = now + timedelta(seconds=1), now + timedelta(seconds=2)
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"mem_used_pct": 95, "collected_at": t1.isoformat()})
        result = watchdog.emit(db, [], {"mem_used_pct": 80, "collected_at": t2.isoformat()})
        assert result["recovered"] == 1

        stale = watchdog.emit(db, [finding], {"mem_used_pct": 95, "collected_at": t1.isoformat()})
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key.startswith(watchdog.WATCHDOG_CONDITION_PREFIX)))
        assert stale["emitted"] == 0 and stale["deduplicated"] == 1
        assert not state.active and state.episode == 1
        assert db.scalar(select(func.count()).select_from(models.Event)) == 2


def test_untimestamped_finding_cannot_reopen_a_recovered_episode():
    from datetime import timedelta
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("WORKER_UNHEALTHY", enums.Severity.HIGH,
                               "Memory at 95%", "Host memory pressure", dedupe="mem-high")
    now = datetime.now(timezone.utc)
    first_seen, recovered = now - timedelta(minutes=2), now - timedelta(minutes=1)
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"mem_used_pct": 95, "collected_at": first_seen.isoformat()})
        watchdog.emit(db, [], {"mem_used_pct": 45, "collected_at": recovered.isoformat()})
        result = watchdog.emit(db, [finding], {"mem_used_pct": 95})
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key.startswith(watchdog.WATCHDOG_CONDITION_PREFIX)))
        assert result["emitted"] == 0 and result["deduplicated"] == 1
        assert not state.active and state.episode == 1
        assert db.scalar(select(func.count()).select_from(models.Event)) == 2


def test_recovered_condition_is_not_routed_after_a_crash(monkeypatch):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe="disk-critical")
    scope = watchdog._watchdog_condition_scope(finding)
    with Session(engine) as db:
        event = models.Event(event_type=finding.event_type, source="watchdog",
                             severity=finding.severity,
                             payload={"condition_key": finding.dedupe, "last_seen_at": now.isoformat()})
        db.add(event)
        db.flush()
        db.add(models.NotificationState(scope_key=scope, active=True, episode=1,
                                        severity=finding.severity.value,
                                        delivery_status="observed", event_id=event.id))
        db.add(models.ControlState(key=watchdog._condition_checkpoint_key(scope),
                                   value={"last_seen_at": now.isoformat()}, updated_by="watchdog"))
        db.commit()

        def no_telegram(session, candidate):
            if candidate.event_type == "WATCHDOG_CONDITION_RECOVERED":
                candidate.notify_reason = "incident recovered without Telegram delivery"
                session.commit()
            return False, None

        monkeypatch.setattr(watchdog.notifier, "dispatch", no_telegram)
        result = watchdog.emit(db, [], {"disk_used_pct": 80,
                                        "collected_at": (now + timedelta(seconds=1)).isoformat()})
        assert result["recovered"] == 1
        assert db.scalar(select(func.count()).select_from(models.Incident)) == 0
        assert db.scalar(select(func.count()).select_from(models.Task)) == 0
        state = db.get(models.NotificationState, scope)
        assert not state.active and state.delivery_status == "recovered"


def test_positive_recovery_sends_telegram_before_closing_condition(monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)

    class RecordingAdapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

    adapter = RecordingAdapter()
    monkeypatch.setattr(watchdog.notifier, "adapter", adapter)
    monkeypatch.setattr(watchdog.worker, "worker_state", lambda _db: {"status": "HEALTHY"})
    finding = watchdog.Finding("WORKER_UNHEALTHY", enums.Severity.HIGH,
                               "Worker heartbeat stale", "Worker is no longer reporting",
                               dedupe="worker-heartbeat")
    scope = watchdog._watchdog_condition_scope(finding)
    first = datetime.now(timezone.utc)
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"collected_at": first.isoformat()})
        condition = db.get(models.NotificationState, scope)
        assert condition.active
        assert len(adapter.sent) == 1

        result = watchdog.emit(db, [], {
            "collected_at": (first + timedelta(seconds=1)).isoformat(),
        })
        condition = db.get(models.NotificationState, scope)
        recovery = db.scalar(select(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED"))
        assert result["recovered"] == 1
        assert len(adapter.sent) == 2
        assert "WATCHDOG_CONDITION_RECOVERED" in adapter.sent[1]
        assert recovery.notified and recovery.notify_reason == "recovery delivered"
        assert not condition.active and condition.delivery_status == "recovered"


def test_condition_recovery_while_route_is_delayed_prevents_stale_work(monkeypatch):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe="disk-critical")
    now = datetime.now(timezone.utc)
    original_route = watchdog.event_router.route
    recovered = False

    def delayed_route(db, event):
        nonlocal recovered
        if not recovered:
            recovered = True
            watchdog.emit(db, [], {"disk_used_pct": 80,
                                   "collected_at": (now + timedelta(seconds=1)).isoformat()})
        return original_route(db, event)

    monkeypatch.setattr(watchdog.event_router, "route", delayed_route)
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"disk_used_pct": 96, "collected_at": now.isoformat()})
        assert db.scalar(select(func.count()).select_from(models.Incident)) == 0
        assert db.scalar(select(func.count()).select_from(models.Task)) == 0
        state = db.scalar(select(models.NotificationState).where(
            models.NotificationState.scope_key == watchdog._watchdog_condition_scope(finding)))
        assert state is not None and not state.active and state.delivery_status == "recovered"


def test_ambiguous_recovery_delivery_is_not_automatically_replayed(monkeypatch):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    calls = []
    monkeypatch.setattr(watchdog.notifier, "dispatch",
                        lambda _db, event: calls.append(event.id) or (False, "ambiguous"))
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe="disk-critical")
    scope = watchdog._watchdog_condition_scope(finding)
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with Session(engine) as db:
        original = models.Event(
            event_type=finding.event_type, source="watchdog", severity=finding.severity,
            payload={"condition_key": finding.dedupe, "condition_scope": scope,
                     "condition_episode": 1, "last_seen_at": observed_at.isoformat()},
        )
        db.add(original)
        db.flush()
        db.add(models.NotificationState(
            scope_key=scope, active=True, episode=1, event_id=original.id,
            delivery_status="sent",
        ))
        recovery = models.Event(event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
                                severity=enums.Severity.INFO,
                                notify_reason="recovery delivery failed or uncertain",
                                payload={"condition_key": finding.dedupe,
                                         "condition_scope": scope,
                                         "resolved_event_id": original.id,
                                         "episode": 1},
                                dedupe_key=watchdog.fingerprint(
                                    "WATCHDOG_CONDITION_RECOVERED", "watchdog", f"{scope}|1"))
        db.add(recovery)
        db.commit()
        # A terminal ambiguous delivery neither gets retried nor releases the
        # active condition. Even fresh healthy evidence cannot create a duplicate.
        watchdog.emit(db, [], {"disk_used_pct": 80,
                               "collected_at": datetime.now(timezone.utc).isoformat()})
        assert calls == []
        state = db.get(models.NotificationState, scope)
        assert state.active and state.episode == 1 and state.delivery_status == "uncertain"
        watchdog.emit(db, [finding], {"disk_used_pct": 96,
                                      "collected_at": (datetime.now(timezone.utc) +
                                                       timedelta(seconds=1)).isoformat()})
        assert calls == []
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED")) == 1
        assert db.get(models.NotificationState, scope).episode == 1


def test_terminal_recoveries_do_not_starve_new_dispatch_intent(monkeypatch):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe="disk-critical")
    scope = watchdog._watchdog_condition_scope(finding)
    dispatched = []

    def dispatch(db, event):
        dispatched.append(event.id)
        event.notified = True
        event.notify_reason = "recovery delivered"
        db.commit()
        return True, None

    monkeypatch.setattr(watchdog.notifier, "dispatch", dispatch)

    with Session(engine) as db:
        for number in range(55):
            db.add(models.Event(
                event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
                severity=enums.Severity.INFO, notified=False,
                notify_reason="recovery delivery failed or uncertain",
                payload={"condition_key": f"old-{number}"},
            ))
        original = models.Event(
            event_type=finding.event_type, source="watchdog", severity=finding.severity,
            payload={"condition_key": finding.dedupe, "condition_scope": scope,
                     "condition_episode": 1},
        )
        db.add(original)
        db.flush()
        db.add(models.NotificationState(
            scope_key=scope, active=True, episode=1, event_id=original.id,
            delivery_status="sent",
        ))
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO, notify_reason="recovery dispatch pending",
            payload={"condition_key": finding.dedupe, "condition_scope": scope,
                     "resolved_event_id": original.id, "episode": 1},
            dedupe_key=watchdog.fingerprint(
                "WATCHDOG_CONDITION_RECOVERED", "watchdog", f"{scope}|1"),
        )
        db.add(recovery)
        db.commit()

        watchdog.emit(db, [], {})

        assert dispatched == [recovery.id]
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED")) == 56
        assert db.get(models.NotificationState, scope).active is False


def test_watchdog_recovery_during_alert_send_does_not_stale_the_route_claim(monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("TASK_FAILED_REPEATEDLY", enums.Severity.CRITICAL,
                               "Task retries exhausted", "Task blocked", dedupe="interleaved-task")
    scope = watchdog._watchdog_condition_scope(finding)
    first_seen = datetime.now(timezone.utc) - timedelta(seconds=2)
    recovered_at = first_seen + timedelta(seconds=1)
    sent = []

    class InterleavingAdapter:
        configured = True
        interleaved = False

        def send(self, message):
            sent.append(message)
            if self.interleaved:
                return
            self.interleaved = True
            with Session(engine) as concurrent:
                watchdog.emit(concurrent, [], {
                    "collected_at": recovered_at.isoformat(),
                })

    monkeypatch.setattr(watchdog.notifier, "adapter", InterleavingAdapter())
    with Session(engine) as db:
        first = watchdog.emit(db, [finding], {
            "collected_at": first_seen.isoformat(),
        })
        assert first["emitted"] == 1
        claim = db.get(models.NotificationState, scope)
        assert claim.delivery_status == "processed"
        recovery = db.scalar(select(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED"))
        assert recovery is not None and recovery.notify_reason == "recovery dispatch pending"

        watchdog.emit(db, [], {
            "collected_at": (recovered_at + timedelta(seconds=1)).isoformat(),
        })
        assert recovery.notified is True and recovery.notify_reason == "recovery delivered"
        assert claim.active is False and claim.delivery_status == "recovered"
        assert len(sent) == 2


def test_founder_can_resume_exhausted_watchdog_recovery_after_source_delivery(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("TASK_FAILED_REPEATEDLY", enums.Severity.CRITICAL,
                               "Task retries exhausted", "Task blocked", dedupe="retry-exhausted")
    condition_scope = watchdog._watchdog_condition_scope(finding)
    sent = []

    class RecordingAdapter:
        configured = True

        def send(self, message):
            sent.append(message)

    monkeypatch.setattr(watchdog.notifier, "adapter", RecordingAdapter())
    with Session(engine) as db:
        original = models.Event(
            event_type=finding.event_type, source="watchdog", severity=finding.severity,
            payload={"condition_key": finding.dedupe, "condition_scope": condition_scope,
                     "condition_episode": 1},
        )
        db.add(original)
        db.flush()
        claim = models.NotificationState(
            scope_key=condition_scope, active=True, episode=1, event_id=original.id,
            delivery_status="processed",
        )
        db.add(claim)
        db.commit()
        assert watchdog.notifier.dispatch(db, original) == (True, None)

        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": finding.dedupe, "condition_scope": condition_scope,
                     "resolved_event_id": original.id, "episode": 1},
            notify_reason="recovery dispatch retries exhausted; founder review required",
        )
        db.add(recovery)
        db.commit()

        assert watchdog._finalize_condition_recovery(db, recovery) is False
        assert claim.active and claim.delivery_status == "processed"
        assert watchdog.notifier.retry_uncertain(db, recovery) == (True, None)
        assert recovery.notify_reason == "recovery delivered"
        assert watchdog._finalize_condition_recovery(db, recovery) is True
        assert not claim.active and claim.delivery_status == "recovered"
        assert len(sent) == 2


def test_malformed_recovery_retry_checkpoint_requires_founder_review(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    dispatches = []
    monkeypatch.setattr(watchdog.notifier, "dispatch",
                        lambda *_: dispatches.append("unexpected") or (False, None))

    with Session(engine) as db:
        recoveries = []
        for title, checkpoint_value in (
            ("missing-time", {"attempts": 1, "next_retry_at": None}),
            ("invalid-time", {"attempts": 1, "next_retry_at": "not-a-date"}),
            ("impossible-time", {"attempts": 1, "next_retry_at": "2026-99-99T99:99:99+00:00"}),
            ("wrong-shape", ["unexpected"]),
        ):
            recovery = models.Event(
                event_type="INCIDENT_RESOLVED", source="company-api",
                severity=enums.Severity.INFO, payload={"title": title},
                notify_reason="recovery dispatch pending",
            )
            db.add(recovery)
            db.flush()
            db.add(models.ControlState(
                key=f"{watchdog.RECOVERY_RETRY_STATE_PREFIX}{recovery.id}",
                value=checkpoint_value, updated_by="watchdog",
            ))
            recoveries.append(recovery)
        db.commit()

        watchdog.emit(db, [], {})

        assert dispatches == []
        for recovery in recoveries:
            assert recovery.notify_reason == \
                "recovery dispatch retries exhausted; founder review required"
            assert "checkpoint" in recovery.notify_error.lower()


def test_watchdog_route_failures_allow_one_bounded_cooldown_redrive(monkeypatch):
    from datetime import timedelta
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe="disk-critical")
    scope = watchdog._watchdog_condition_scope(finding)
    attempts = []

    def fail_route(*_args):
        attempts.append(1)
        raise RuntimeError("temporary route failure")

    monkeypatch.setattr(watchdog.event_router, "route", fail_route)
    monkeypatch.setattr(watchdog.notifier, "dispatch", lambda *_args: (False, None))
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"disk_used_pct": 96})
        for _ in range(2):
            state = db.get(models.NotificationState, scope)
            state.attempted_at = datetime.now(timezone.utc) - timedelta(minutes=6)
            db.commit()
            watchdog.emit(db, [finding], {"disk_used_pct": 96})

        state = db.get(models.NotificationState, scope)
        assert state.delivery_status == "route_failed"
        assert len(attempts) == 3
        state.attempted_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        watchdog.emit(db, [finding], {"disk_used_pct": 96})
        assert len(attempts) == 4
        route_attempt = db.get(models.ControlState, f"watchdog-route:{state.event_id}")
        assert route_attempt.value["redrives"] == 1 and route_attempt.value["attempts"] == 1
        for _ in range(2):
            state = db.get(models.NotificationState, scope)
            state.attempted_at = datetime.now(timezone.utc) - timedelta(minutes=6)
            db.commit()
            watchdog.emit(db, [finding], {"disk_used_pct": 96})
        assert len(attempts) == 6
        route_attempt = db.get(models.ControlState, f"watchdog-route:{state.event_id}")
        assert route_attempt.value["redrives"] == 1 and route_attempt.value["attempts"] == 3
        state = db.get(models.NotificationState, scope)
        state.attempted_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        watchdog.emit(db, [finding], {"disk_used_pct": 96})
        assert len(attempts) == 6


def test_watchdog_missing_service_route_is_bounded_and_visible():
    from datetime import timedelta
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app import models, watchdog

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    finding = watchdog.Finding("SERVICE_DOWN", enums.Severity.HIGH,
                               "unknown: service down", "Missing service registration",
                               service_key="missing-service", dedupe="down:missing-service")
    scope = watchdog._watchdog_condition_scope(finding)
    with Session(engine) as db:
        watchdog.emit(db, [finding])
        for _ in range(2):
            state = db.get(models.NotificationState, scope)
            state.attempted_at = datetime.now(timezone.utc) - timedelta(minutes=6)
            db.commit()
            watchdog.emit(db, [finding])

        state = db.get(models.NotificationState, scope)
        route_attempt = db.get(models.ControlState, f"watchdog-route:{state.event_id}")
        assert state.delivery_status == "route_failed"
        assert route_attempt.value["attempts"] == 3
        assert route_attempt.value["last_error"] == "LookupError"
        assert db.scalar(select(models.Incident.id)) is None
        assert db.scalar(select(models.Task.id)) is None

        service = models.Service(key="missing-service", name="Recovered registry entry",
                                 container="missing-service", status=enums.ServiceStatus.ACTIVE)
        db.add(service)
        db.commit()
        resumed = watchdog.emit(db, [finding])
        state = db.get(models.NotificationState, scope)
        assert resumed["emitted"] == 0 and state.delivery_status == "processed"
        assert route_attempt.value["redrives"] == 1 and route_attempt.value["attempts"] == 0
        assert db.scalar(select(models.Incident.id)) is not None
        assert db.scalar(select(models.Task.id)) is not None
        source_event = db.get(models.Event, state.event_id)
        assert source_event.service_id is None
        assert source_event.payload["service_key"] == "missing-service"

        watchdog.emit(db, [], {"containers": {}, "containers_complete": True,
                                "collected_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()})
        watchdog.emit(db, [finding])
        assert route_attempt.value["redrives"] == 1


def test_stale_task_count_does_not_change_condition_identity():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import models
    from app.watchdog import check_stale_work

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    old = datetime.now(timezone.utc) - timedelta(days=5)
    with Session(engine) as db:
        for number in (1, 2):
            db.add(models.Task(key=f"TASK-{number:04d}", title="old", success_criteria="reviewed",
                               status=enums.TaskStatus.BLOCKED,
                               priority=enums.Priority.P2, risk_level=enums.RiskLevel.LOW,
                               created_at=old))
            db.commit()
            finding = next(f for f in check_stale_work(db) if "BLOCKED" in f.title)
            assert finding.dedupe == "stale:BLOCKED"
