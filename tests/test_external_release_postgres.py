"""Optional real PostgreSQL lock checks; only the disposable test database is allowed."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier, Event as ThreadEvent, get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event as sqlalchemy_event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import enums, event_router, models, notify, watchdog, worker
from tests.test_external_release import NOW, terminal, waiting
from tests.test_worker import SPECS, FakeProvider, _good, _task


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Agent(key="research", role="research", enabled=True)); db.commit()
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(worker, "ENABLE_AUTOMATIC_EXTERNAL_RELEASE", True)
    monkeypatch.setattr(worker, "_now", lambda: NOW)


@pytest.fixture
def notification_service_id(engine):
    with Session(engine) as db:
        service = models.Service(
            key=f"notification-test-{uuid4()}", name="Watchdog notification test service",
            status=enums.ServiceStatus.ACTIVE, risk=enums.RiskLevel.LOW,
        )
        db.add(service)
        db.commit()
        return service.id


def _wait_for_pg_block(engine, waiter_pid, blocker_pid, timeout=10):
    deadline = monotonic() + timeout
    query = text("""
        SELECT wait_event_type = 'Lock'
           AND :blocker_pid = ANY(pg_blocking_pids(pid))
        FROM pg_stat_activity WHERE pid = :waiter_pid
    """)
    while monotonic() < deadline:
        with engine.connect() as connection:
            blocked = connection.execute(query, {
                "waiter_pid": waiter_pid, "blocker_pid": blocker_pid,
            }).scalar_one_or_none()
        if blocked:
            return
        sleep(0.02)
    pytest.fail(f"PostgreSQL did not report pid {waiter_pid} blocked by {blocker_pid}")


def _saved_worker_result(engine, monkeypatch, key):
    with Session(engine) as db:
        task = _task(db, key=key, priority=enums.Priority.P0)
        task_id = task.id
        with monkeypatch.context() as fault:
            def crash(*args, **kwargs):
                raise SystemExit("process terminated after saving its result")

            fault.setattr(worker.task_materializer, "materialize", crash)
            with pytest.raises(SystemExit):
                worker.run_once(db, {"claude": FakeProvider("claude", [_good(
                    key, learning_candidates=[f"Preserve the completed result of {key} before routing work."])])}, SPECS)
        db.rollback()
        run_id = db.scalar(select(models.AgentRun.id).where(models.AgentRun.task_id == task_id))
    monkeypatch.setattr(worker, "_now", lambda: NOW + worker.STALE_AFTER + timedelta(seconds=5))
    return task_id, run_id


def test_concurrent_workers_finalize_one_saved_result_once(engine, monkeypatch):
    task_id, run_id = _saved_worker_result(engine, monkeypatch, "TASK-9970")
    barrier = Barrier(2)

    def resume():
        with Session(engine) as db:
            barrier.wait(timeout=5)
            return worker._resume_result(db, run_id, SPECS)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resume) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]
    assert sum(result is not None for result in results) == 1
    with Session(engine) as db:
        assert db.get(models.Task, task_id).status == enums.TaskStatus.REVIEW
        assert db.get(models.AgentRun, run_id).status == enums.AgentRunStatus.SUCCEEDED
        assert db.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.task_id == task_id, models.AuditLog.action == "task.autonomous-run")) == 1
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.task_id == task_id, models.Event.event_type == "TASK_COMPLETED")) == 1
        assert db.scalar(select(models.AgentNote.reference_count).where(models.AgentNote.task_id == task_id)) == 1


def test_locked_saved_result_does_not_hold_another_worker(engine, monkeypatch):
    task_id, run_id = _saved_worker_result(engine, monkeypatch, "TASK-9971")
    holder = Session(engine)
    holder.scalar(select(models.Task).where(models.Task.id == task_id).with_for_update())

    def resume():
        with Session(engine) as db:
            return worker._resume_result(db, run_id, SPECS)

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(resume).result(timeout=2) is None
    finally:
        holder.rollback()
        holder.close()
        pool.shutdown(wait=True)
    assert resume()["verdict"] == "APPROVED"


def test_result_owner_keeps_finalizing_after_a_competing_inspection(engine, monkeypatch):
    task_id, run_id = _saved_worker_result(engine, monkeypatch, "TASK-9972")
    claimed, inspect = ThreadEvent(), ThreadEvent()
    original = worker._result_rows
    calls, owner_pid = [], []

    def rows(db, *args, **kwargs):
        calls.append(None)
        if len(calls) == 2:
            owner_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            claimed.set()
            assert inspect.wait(timeout=5)
        return original(db, *args, **kwargs)

    monkeypatch.setattr(worker, "_result_rows", rows)

    def resume():
        with Session(engine) as db:
            return worker._resume_result(db, run_id, SPECS)

    with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as holder:
        future = pool.submit(resume)
        assert claimed.wait(timeout=5)
        holder.scalar(select(models.Task).where(models.Task.id == task_id).with_for_update())
        blocker_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        inspect.set()
        try:
            _wait_for_pg_block(engine, owner_pid[0], blocker_pid, timeout=2)
        finally:
            holder.rollback()
        assert future.result(timeout=5)["verdict"] == "APPROVED"


def test_two_controllers_release_one_task_once(engine):
    with Session(engine) as db:
        task = waiting(db, key="TASK-9001"); terminal(db)
        key, task_id = task.key, task.id
    barrier = Barrier(2)
    def release():
        with Session(engine) as db:
            barrier.wait(timeout=5)
            return worker.release_external_waits(db, SPECS)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: release(), range(2)))
    assert sorted(results) == [[], [key]]
    with Session(engine) as db:
        assert db.get(models.Task, task_id).status == enums.TaskStatus.READY
        assert db.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.task_id==task_id, models.AuditLog.action=="task.external_release")) == 1
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.task_id==task_id, models.Event.event_type=="EXTERNAL_WAIT_RELEASED")) == 1


def test_locked_wait_does_not_block_unrelated_release(engine):
    with Session(engine) as db:
        blocked = waiting(db, key="TASK-9002")
        free = waiting(db, key="TASK-9003")
        terminal(db, occurred_at=NOW-timedelta(seconds=1))
        blocked_id, free_id, free_key = blocked.id, free.id, free.key
    lock = Session(engine)
    lock.execute(select(models.Task).where(models.Task.id==blocked_id).with_for_update()).scalar_one()
    def release():
        with Session(engine) as db: return worker.release_external_waits(db, SPECS)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(release)
        assert future.result(timeout=2) == [free_key]
        with Session(engine) as db:
            assert db.get(models.Task, blocked_id).status == enums.TaskStatus.WAITING_EXTERNAL
            assert db.get(models.Task, free_id).status == enums.TaskStatus.READY
    finally:
        lock.rollback(); lock.close(); pool.shutdown(wait=True)


def test_budget_exhaustion_does_not_keep_proved_condition_waiting(engine):
    from dataclasses import replace
    specs = {**SPECS, "research": replace(SPECS["research"], budget={"max_runs_per_day": 1})}
    with Session(engine) as db:
        task = waiting(db, key="TASK-9004", retry_count=1, result="previous result")
        db.add(models.AgentRun(agent_id=task.assigned_agent_id, task_id=task.id, provider="claude",
            status=enums.AgentRunStatus.SUCCEEDED, started_at=NOW-timedelta(minutes=1), ended_at=NOW))
        db.commit(); event = terminal(db)
        out = worker.run_once(db, {}, specs); db.refresh(task)
        assert task.status == enums.TaskStatus.READY and out["model_calls"] == 0
        assert task.retry_count == 1 and task.result == "previous result"
        assert task.context["external_release"]["event_id"] == event.id
        assert worker._eligible(db, task, specs)[0] == "daily_run_budget"
        assert db.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.task_id == task.id, models.AuditLog.action == "task.external_release")) == 1


def test_concurrent_watchdog_ticks_record_one_condition_event(engine):
    dedupe = f"deadletter:{uuid4()}"
    finding = watchdog.Finding("TASK_FAILED_REPEATEDLY", enums.Severity.HIGH,
                               "exhausted task retries", "test-only condition", dedupe=dedupe)
    barrier = Barrier(4)

    def tick(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return watchdog.emit(db, [finding])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tick, range(4)))

    assert sum(result["emitted"] for result in results) == 1
    with Session(engine) as db:
        fingerprint = watchdog.fingerprint(finding.event_type, "watchdog", dedupe)
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.dedupe_key == fingerprint)) == 1


def test_concurrent_watchdog_recovery_retries_claim_one_cooldown_attempt(engine):
    recovery = models.Event(
        event_type="INCIDENT_RESOLVED", source="company-api", severity=enums.Severity.INFO,
        payload={"title": f"resolved-{uuid4()}"}, notify_reason="recovery dispatch pending",
    )
    with Session(engine) as db:
        db.add(recovery)
        db.commit()
        recovery_id = recovery.id

    pids = []
    barrier = Barrier(2)

    def claim_retry():
        with Session(engine) as db:
            pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            pids.append(pid)
            barrier.wait(timeout=5)
            stale_recovery = db.get(models.Event, recovery_id)
            assert stale_recovery is not None
            # Both transactions read the same event before either claim starts.
            # The claim's row lock must serialize these stale callers so one
            # persists the attempt and the other observes its cooldown.
            barrier.wait(timeout=5)
            return watchdog._start_recovery_retry(db, stale_recovery)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_retry) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert len(set(pids)) == 2, "retry workers did not use separate PostgreSQL sessions"

    assert sorted(result for result in results if result is not None) == [1]
    with Session(engine) as db:
        checkpoint = db.get(models.ControlState, f"{watchdog.RECOVERY_RETRY_STATE_PREFIX}{recovery_id}")
        assert checkpoint.value["attempts"] == 1


def test_concurrent_old_finding_cannot_reopen_recovered_condition(engine):
    dedupe = f"disk-critical:{uuid4()}"
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe=dedupe)
    scope = watchdog._watchdog_condition_scope(finding)
    now = datetime.now(timezone.utc)
    old_observation = now + timedelta(seconds=1)
    recovered_observation = now + timedelta(seconds=2)
    with Session(engine) as db:
        watchdog.emit(db, [finding], {"disk_used_pct": 96, "collected_at": now.isoformat()})

    barrier = Barrier(2)

    def tick(is_recovery):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            if is_recovery:
                return watchdog.emit(db, [], {"disk_used_pct": 80,
                                               "collected_at": recovered_observation.isoformat()})
            return watchdog.emit(db, [finding], {"disk_used_pct": 96,
                                                  "collected_at": old_observation.isoformat()})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(tick, (True, False)))

    with Session(engine) as db:
        state = db.get(models.NotificationState, scope)
        assert state is not None and state.active is False and state.episode == 1
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED",
            models.Event.dedupe_key == watchdog.fingerprint(
                "WATCHDOG_CONDITION_RECOVERED", "watchdog", f"{scope}|1"))) == 1


def test_route_claim_serializes_against_condition_recovery(engine, monkeypatch):
    dedupe = f"disk-critical:{uuid4()}"
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe=dedupe)
    scope = watchdog._watchdog_condition_scope(finding)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        service = models.Service(key=f"disk-test-{uuid4()}", name="Watchdog test service",
                                 status=enums.ServiceStatus.ACTIVE, risk=enums.RiskLevel.LOW)
        db.add(service)
        db.flush()
        service_id = service.id
        event = models.Event(
            event_type=finding.event_type, source="watchdog", severity=finding.severity,
            service_id=service_id,
            payload={"title": finding.title, "detail": finding.detail,
                     "condition_key": finding.dedupe, "condition_scope": scope,
                     "condition_episode": 1, "episode": 1},
            dedupe_key=watchdog.fingerprint(finding.event_type, "watchdog", dedupe),
        )
        db.add(event)
        db.flush()
        event_id = event.id
        db.add(models.NotificationState(
            scope_key=scope, active=True, episode=1, event_id=event_id,
            delivery_status="processing", attempted_at=now,
        ))
        db.commit()

    route_entered, recovery_lock_attempted = ThreadEvent(), ThreadEvent()
    release_route, recovery_finished = ThreadEvent(), ThreadEvent()
    recovery_thread_id = None
    route_pid = None
    recovery_pid = None
    original_add = event_router.keys.add_with_key

    def observe_recovery_lock(conn, cursor, statement, parameters, context, executemany):
        if (get_ident() == recovery_thread_id and "notification_states" in statement.lower()
                and "for update" in statement.lower()):
            recovery_lock_attempted.set()

    sqlalchemy_event.listen(engine, "before_cursor_execute", observe_recovery_lock)

    def pause_incident_insert(db, obj, prefix, width=4):
        if isinstance(obj, models.Incident):
            route_entered.set()
            if not release_route.wait(timeout=10):
                raise TimeoutError("test did not release the route insert")
        return original_add(db, obj, prefix, width)

    monkeypatch.setattr(event_router.keys, "add_with_key", pause_incident_insert)

    def route_condition():
        nonlocal route_pid
        with Session(engine) as db:
            route_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            event = db.get(models.Event, event_id)
            return event_router.route(db, event)

    def recover_condition():
        nonlocal recovery_thread_id, recovery_pid
        recovery_thread_id = get_ident()
        try:
            with Session(engine) as db:
                recovery_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
                return watchdog.emit(db, [], {
                    "disk_used_pct": 80,
                    "collected_at": (now + timedelta(seconds=1)).isoformat(),
                })
        finally:
            recovery_finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            route_future = pool.submit(route_condition)
            assert route_entered.wait(timeout=10)
            recovery_future = pool.submit(recover_condition)
            assert recovery_lock_attempted.wait(timeout=10), "recovery never attempted the condition row lock"
            _wait_for_pg_block(engine, recovery_pid, route_pid)
            assert not recovery_finished.is_set(), "recovery passed a route that held the condition lock"
            release_route.set()
            routed = route_future.result(timeout=10)
            recovered = recovery_future.result(timeout=10)
    finally:
        release_route.set()
        sqlalchemy_event.remove(engine, "before_cursor_execute", observe_recovery_lock)

    assert routed is not None and recovered["recovered"] == 1
    with Session(engine) as db:
        state = db.get(models.NotificationState, scope)
        assert state is not None and not state.active and state.episode == 1
        assert db.scalar(select(func.count()).select_from(models.Incident).where(
            models.Incident.open_scope == f"service:{service_id}")) == 1
        tasks = db.scalars(select(models.Task).where(models.Task.created_by == "event-router")).all()
        assert sum(task.context.get("source_event") == event_id for task in tasks) == 1


def test_recovery_wins_before_stale_route_claim(engine, monkeypatch):
    dedupe = f"disk-critical:{uuid4()}"
    finding = watchdog.Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               "Disk at 96%", "Root filesystem critical", dedupe=dedupe)
    scope = watchdog._watchdog_condition_scope(finding)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        service = models.Service(key=f"disk-test-{uuid4()}", name="Watchdog test service",
                                 status=enums.ServiceStatus.ACTIVE, risk=enums.RiskLevel.LOW)
        db.add(service)
        db.flush()
        service_id = service.id
        event = models.Event(
            event_type=finding.event_type, source="watchdog", severity=finding.severity,
            service_id=service_id,
            payload={"title": finding.title, "detail": finding.detail,
                     "condition_key": finding.dedupe, "condition_scope": scope,
                     "condition_episode": 1, "episode": 1},
            dedupe_key=watchdog.fingerprint(finding.event_type, "watchdog", dedupe),
        )
        db.add(event)
        db.flush()
        event_id = event.id
        db.add(models.NotificationState(
            scope_key=scope, active=True, episode=1, event_id=event_id,
            delivery_status="processing", attempted_at=now,
        ))
        db.commit()

    route_lock_attempted, release_route, route_finished = ThreadEvent(), ThreadEvent(), ThreadEvent()
    route_thread_id = None

    def pause_route_claim(conn, cursor, statement, parameters, context, executemany):
        if (get_ident() == route_thread_id and "notification_states" in statement.lower()
                and "for update" in statement.lower()):
            route_lock_attempted.set()
            if not release_route.wait(timeout=10):
                raise TimeoutError("test did not release the stale route claim")

    sqlalchemy_event.listen(engine, "before_cursor_execute", pause_route_claim)

    def route_condition():
        nonlocal route_thread_id
        route_thread_id = get_ident()
        try:
            with Session(engine) as db:
                event = db.get(models.Event, event_id)
                return event_router.route(db, event)
        finally:
            route_finished.set()

    def recover_condition():
        with Session(engine) as db:
            return watchdog.emit(db, [], {
                "disk_used_pct": 80,
                "collected_at": (now + timedelta(seconds=1)).isoformat(),
            })

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            route_future = pool.submit(route_condition)
            assert route_lock_attempted.wait(timeout=10), "route never reached its condition claim"
            recovery_future = pool.submit(recover_condition)
            recovered = recovery_future.result(timeout=10)
            assert recovered["recovered"] == 1
            assert not route_finished.is_set(), "route was not held before the condition claim"
            release_route.set()
            routed = route_future.result(timeout=10)
    finally:
        release_route.set()
        sqlalchemy_event.remove(engine, "before_cursor_execute", pause_route_claim)

    assert routed is None
    with Session(engine) as db:
        state = db.get(models.NotificationState, scope)
        assert state is not None and not state.active and state.episode == 1
        assert db.scalar(select(func.count()).select_from(models.Incident).where(
            models.Incident.open_scope == f"service:{service_id}")) == 0
        assert db.scalar(select(func.count()).select_from(models.Task).where(
            models.Task.created_by == "event-router",
            models.Task.context["source_event"].as_integer() == event_id)) == 0


def test_recovery_delivery_holds_condition_order_against_new_episode(engine, notification_service_id):
    scope = f"watchdog-condition:recovery-order:{uuid4()}"
    recovery_send_entered, release_recovery = ThreadEvent(), ThreadEvent()

    class BlockingAdapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)
            if "WATCHDOG_CONDITION_RECOVERED" in message:
                recovery_send_entered.set()
                if not release_recovery.wait(timeout=10):
                    raise TimeoutError("test did not release recovery delivery")

    adapter = BlockingAdapter()
    service = notify.NotificationService(adapter)
    original = models.Event(
        event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
        service_id=notification_service_id,
        payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                 "condition_episode": 1},
    )
    scope_key = service.scope(original)
    recovery = models.Event(
        event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
        severity=enums.Severity.INFO,
        service_id=notification_service_id,
        payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                 "resolved_event_id": None, "episode": 1},
    )
    with Session(engine) as db:
        db.add(original)
        db.commit()
        assert service.dispatch(db, original) == (True, None)
        recovery.payload = {**recovery.payload, "resolved_event_id": original.id}
        db.add(recovery)
        db.commit()
        original_id, recovery_id = original.id, recovery.id

    recovery_pid = renewal_pid = None

    def dispatch_recovery():
        nonlocal recovery_pid
        with Session(engine) as db:
            recovery_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            return service.dispatch(db, db.get(models.Event, recovery_id))

    def dispatch_renewal():
        nonlocal renewal_pid
        with Session(engine) as db:
            renewed = models.Event(
                event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
                service_id=notification_service_id,
                payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                         "condition_episode": 2},
            )
            db.add(renewed)
            db.commit()
            # Commit returns the connection to the pool. Observe the backend of
            # the transaction that will actually acquire the notification lock.
            renewal_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            return service.dispatch(db, renewed)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovery_future = pool.submit(dispatch_recovery)
            assert recovery_send_entered.wait(timeout=10), "recovery send did not start"
            renewal_future = pool.submit(dispatch_renewal)
            deadline = monotonic() + 10
            while renewal_pid is None and monotonic() < deadline:
                sleep(0.01)
            assert renewal_pid is not None, "renewal backend did not start"
            _wait_for_pg_block(engine, renewal_pid, recovery_pid)
            release_recovery.set()
            assert recovery_future.result(timeout=10) == (True, None)
            assert renewal_future.result(timeout=10) == (True, None)
    finally:
        release_recovery.set()

    assert len(adapter.sent) == 3
    assert "WATCHDOG_CONDITION_RECOVERED" in adapter.sent[1]
    assert "WORKER_UNHEALTHY" in adapter.sent[2]
    with Session(engine) as db:
        state = db.get(models.NotificationState, scope_key)
        assert state is not None and state.active and state.episode == 2
        assert state.event_id != original_id and state.delivery_status == "sent"
        stored_recovery = db.get(models.Event, recovery_id)
        assert stored_recovery.notified and stored_recovery.notify_reason == "recovery delivered"


def test_concurrent_recovery_dispatchers_preserve_one_delivery_evidence(engine, notification_service_id):
    scope = f"watchdog-condition:recovery-dedupe:{uuid4()}"
    recovery_send_entered, recovery_loaded = ThreadEvent(), ThreadEvent()
    recovery_lock_attempted, allow_second_dispatch = ThreadEvent(), ThreadEvent()
    release_recovery = ThreadEvent()

    class BlockingAdapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, message):
            if "WATCHDOG_CONDITION_RECOVERED" in message:
                recovery_send_entered.set()
                if not release_recovery.wait(timeout=10):
                    raise TimeoutError("test did not release recovery delivery")
            self.sent.append(message)

    adapter = BlockingAdapter()
    service = notify.NotificationService(adapter)
    original = models.Event(
        event_type="WORKER_UNHEALTHY", source="watchdog", severity=enums.Severity.HIGH,
        service_id=notification_service_id,
        payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                 "condition_episode": 1},
    )
    with Session(engine) as db:
        db.add(original)
        db.commit()
        assert service.dispatch(db, original) == (True, None)
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            service_id=notification_service_id,
            payload={"condition_key": "worker-heartbeat", "condition_scope": scope,
                     "resolved_event_id": original.id, "episode": 1},
            notify_reason="recovery dispatch pending",
        )
        db.add(recovery)
        db.commit()
        recovery_id = recovery.id

    first_pid = second_pid = None
    second_thread_id = None

    def observe_second_lock(conn, cursor, statement, parameters, context, executemany):
        if (get_ident() == second_thread_id and "notification_states" in statement.lower()
                and "for update" in statement.lower()):
            recovery_lock_attempted.set()

    sqlalchemy_event.listen(engine, "before_cursor_execute", observe_second_lock)

    def first_dispatch():
        nonlocal first_pid
        with Session(engine) as db:
            first_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            return service.dispatch(db, db.get(models.Event, recovery_id))

    def second_dispatch():
        nonlocal second_pid, second_thread_id
        second_thread_id = get_ident()
        with Session(engine) as db:
            second_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            stale_recovery = db.get(models.Event, recovery_id)
            recovery_loaded.set()
            if not allow_second_dispatch.wait(timeout=10):
                raise TimeoutError("test did not release the second dispatcher")
            return service.dispatch(db, stale_recovery)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first_dispatch)
            assert recovery_send_entered.wait(timeout=10), "first recovery send did not start"
            second_future = pool.submit(second_dispatch)
            assert recovery_loaded.wait(timeout=10), "second dispatcher did not cache the pending event"
            allow_second_dispatch.set()
            assert recovery_lock_attempted.wait(timeout=10), "second dispatch never reached the scope lock"
            _wait_for_pg_block(engine, second_pid, first_pid)
            release_recovery.set()
            assert first_future.result(timeout=10) == (True, None)
            assert second_future.result(timeout=10) == (True, "recovery delivered")
    finally:
        release_recovery.set()
        allow_second_dispatch.set()
        sqlalchemy_event.remove(engine, "before_cursor_execute", observe_second_lock)

    assert len(adapter.sent) == 2
    with Session(engine) as db:
        stored = db.get(models.Event, recovery_id)
        assert stored.notified and stored.notify_reason == "recovery delivered"
