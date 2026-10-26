"""The reconciler's execution evidence is what the watchdog observed, never inferred from created tasks."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import business_reconciler as br, desk, models, registry, reconciler_heartbeat as hb, watchdog
from tests.test_business_reconciler import NOW, _db, _goal, _observe

NOW_ = datetime.now(timezone.utc)


def test_no_heartbeat_is_no_evidence_not_success():
    db = _db()
    v = hb.view(db)
    assert (v["outcome"], v["freshness"], v["last_attempt_at"], v["tasks_created"]) == (
        "NO_EVIDENCE", "UNAVAILABLE", None, None)


def test_runs_with_zero_tasks_is_completed_evidence():
    db = _db()  # no goals: reconcile executes and creates nothing
    assert hb.run(db, br.reconcile) == []
    v = hb.view(db)
    assert v["outcome"] == "COMPLETED" and v["tasks_created"] == 0 and v["freshness"] == "VERIFIED"
    assert v["last_completed_at"] and v["last_attempt_at"] and v["duration_ms"] >= 0
    # Structured evidence: zero ACTIVE goals examined, zero failed. "OK" says exactly that and no more.
    assert v["evidence_source"] == "watchdog" and (v["per_goal_outcome"], v["goals_total"]) == ("OK", 0)


def test_records_created_tasks_and_dedup_is_unchanged():
    db = _db()
    _goal(db)
    _observe(db, 3500)
    first = hb.run(db, br.reconcile)
    assert len(first) == 1 and hb.view(db)["tasks_created"] == 1
    for _ in range(3):  # repeated ticks
        assert hb.run(db, br.reconcile) == []
    v = hb.view(db)
    assert v["tasks_created"] == 0 and v["outcome"] == "COMPLETED"
    assert db.scalar(select(func.count()).select_from(models.Task)) == 1
    assert db.scalar(select(func.count()).select_from(models.AuditLog)
                     .where(models.AuditLog.entity_type == "control_state")) == 0  # no audit churn
    assert db.get(models.ControlState, hb.KEY).value["created_task_keys"] == []


def test_created_keys_are_bounded():
    db = _db()
    hb.run(db, lambda d: [f"TASK-{i}" for i in range(50)])
    value = db.get(models.ControlState, hb.KEY).value
    assert value["created_task_count"] == 50 and len(value["created_task_keys"]) == hb.MAX_KEYS


def test_exception_is_recorded_and_still_raised_once():
    db = _db()
    hb.run(db, lambda d: [])
    good = hb.view(db)["last_completed_at"]
    calls = []

    def boom(d):
        calls.append(1)
        raise RuntimeError("secret detail must not be stored")
    with pytest.raises(RuntimeError):
        hb.run(db, boom)
    v = hb.view(db)
    assert calls == [1] and v["outcome"] == "RAISED" and v["error"] == "RuntimeError"
    assert v["last_completed_at"] == good and v["tasks_created"] is None  # last good completion is kept
    assert "secret" not in str(db.get(models.ControlState, hb.KEY).value)


def test_partial_goal_failure_is_not_claimed_as_success(monkeypatch):
    db = _db()
    _goal(db)

    def bad(*a, **k):
        raise ValueError("one goal broke")
    monkeypatch.setattr(br, "findings", bad)
    assert hb.run(db, br.reconcile) == []  # reconcile swallows per-goal errors
    v = hb.view(db)
    assert v["outcome"] == "COMPLETED"  # the invocation completed...
    assert v["per_goal_outcome"] == "FAILED" and v["goal_errors"] == 1  # ...but a goal did not
    [item] = desk.needs_you([], [], [], {}, None, False, reconciler=v)[1:]  # [0] is the missing backup
    assert item["kind"] == "OTHER" and "1 goal" in item["title"]
    monkeypatch.undo()
    hb.run(db, br.reconcile)  # the next clean tick clears it, from reconcile's own per-goal evidence
    v = hb.view(db)
    assert (v["per_goal_outcome"], v["goal_errors"], v["goals_total"]) == ("OK", 0, 1)


def test_ok_needs_structured_evidence_a_plain_list_stays_unknown():
    db = _db()
    hb.run(db, lambda d: [])          # a callable that reports no per-goal evidence: no log line is not success
    v = hb.view(db)
    assert (v["per_goal_outcome"], v["goals_total"]) == ("unknown", None)


def test_inconsistent_or_partial_evidence_is_never_ok():
    db = _db()
    hb.run(db, lambda d: br.Reconciled([], goals_total=1, failed_goal_ids=[1, 2]))   # more failures than goals
    assert hb.view(db)["per_goal_outcome"] == "unknown"
    hb.run(db, lambda d: br.Reconciled([], goals_total=3, failed_goal_ids=[2]))
    v = hb.view(db)
    assert (v["per_goal_outcome"], v["goal_errors"], v["goals_total"]) == ("FAILED", 1, 3)
    assert db.get(models.ControlState, hb.KEY).value["failed_goal_ids"] == [2]
    # a stored "OK" that is not a completed invocation with a total and zero errors is not believed
    for value in ({"outcome": "STARTED", "per_goal_outcome": "OK", "goal_errors": 0, "goals_total": 1},
                  {"outcome": "COMPLETED", "per_goal_outcome": "OK", "goal_errors": 0},
                  {"outcome": "COMPLETED", "per_goal_outcome": "OK", "goal_errors": 2, "goals_total": 4}):
        at = datetime.now(timezone.utc).isoformat()
        _store(db, {"last_attempt_at": at, "last_completed_at": at, **value})
        assert hb.view(db)["per_goal_outcome"] != "OK", value


def test_reconcile_still_returns_a_list_of_keys():
    db = _db()
    out = br.reconcile(db)
    assert out == [] and isinstance(out, list) and out.goals_total == 0 and out.failed_goal_ids == []


def test_goal_errors_on_other_threads_are_not_counted():
    import logging
    import threading
    db = _db()

    def reconcile(d):
        t = threading.Thread(target=lambda: logging.getLogger(hb.GOAL_LOGGER).error("other thread"))
        t.start()
        t.join()
        return []
    hb.run(db, reconcile)
    assert hb.view(db)["goal_errors"] == 0
    assert all(not isinstance(h, hb._GoalErrors) for h in logging.getLogger(hb.GOAL_LOGGER).handlers)


def test_goal_errors_are_kept_when_reconcile_raises():
    import logging
    db = _db()

    def reconcile(d):
        logging.getLogger(hb.GOAL_LOGGER).error("business reconcile failed for goal %s", 1)
        raise RuntimeError("late")
    with pytest.raises(RuntimeError):
        hb.run(db, reconcile)
    v = hb.view(db)
    assert (v["outcome"], v["goal_errors"], v["per_goal_outcome"]) == ("RAISED", 1, "FAILED")
    assert not logging.getLogger(hb.GOAL_LOGGER).handlers


@pytest.mark.parametrize("value", [-1, True, "2", 1.5])
def test_untrusted_goal_error_counts_are_dropped(value):
    db = _store_db({"last_attempt_at": NOW_.isoformat(), "last_completed_at": NOW_.isoformat(),
                    "outcome": "COMPLETED", "goal_errors": value, "per_goal_outcome": "FAILED"})
    v = hb.view(db)
    assert v["goal_errors"] is None and v["per_goal_outcome"] == "unknown"


def _store_db(value):
    db = _db()
    _store(db, value)
    return db


@pytest.mark.parametrize("value, title", [
    (None, "not verifiably running"),  # no evidence at all
    ({"last_attempt_at": (NOW_ - timedelta(hours=3)).isoformat(), "outcome": "COMPLETED",
      "last_completed_at": (NOW_ - timedelta(hours=3)).isoformat()}, "not verifiably running"),  # stale
    ({"last_attempt_at": (NOW_ - timedelta(minutes=30)).isoformat(), "outcome": "STARTED"},
     "not verifiably running"),  # unfinished (crash)
    ({"last_attempt_at": NOW_.isoformat(), "outcome": "RAISED", "error": "OperationalError"},
     "not verifiably running"),
    ({"last_attempt_at": "garbage", "outcome": "COMPLETED"}, "not verifiably running"),
    ({"last_attempt_at": NOW_.isoformat(), "last_completed_at": NOW_.isoformat(), "outcome": "COMPLETED"}, None),
    ({"last_attempt_at": NOW_.isoformat(), "outcome": "STARTED"}, None),  # in flight, fresh
])
def test_founder_sees_unhealthy_reconciler_execution(value, title, monkeypatch):
    monkeypatch.setattr(hb, "_now", lambda: NOW_)
    db = _db()
    if value is not None:
        _store(db, value)
    state = desk.build_state(db, registry.load(), False)
    found = [i for i in state["needs_you"] if i["target"]["page"] == "health" and "reconciler" in i["title"]]
    if title is None:
        assert found == []
    else:
        [item] = found
        assert title in item["title"] and item["kind"] == "STOPPED"
        assert state["reconciler_execution"]["outcome"] in item["evidence"]


def test_needs_you_without_reconciler_evidence_is_unchanged():
    assert desk.needs_you([], [], [], {}, None, False) == desk.needs_you([], [], [], {}, None, False, reconciler=None)


def test_heartbeat_write_failure_does_not_change_reconcile(monkeypatch):
    db = _db()
    monkeypatch.setattr(hb, "_locked", lambda s: (_ for _ in ()).throw(OSError("db down")))
    assert hb.run(db, lambda d: ["TASK-1"]) == ["TASK-1"]  # evidence lost, reconcile result untouched
    assert hb.view(db)["outcome"] == "NO_EVIDENCE"  # and nothing was invented


def _store(db, value):
    db.merge(models.ControlState(key=hb.KEY, value=value, updated_by="t"))
    db.commit()


def test_old_malformed_and_unfinished_evidence():
    db = _db()
    old = (NOW_ - timedelta(hours=3)).isoformat()
    _store(db, {"last_attempt_at": old, "last_completed_at": old, "outcome": "COMPLETED", "created_task_count": 2})
    assert hb.view(db)["freshness"] == "STALE"
    _store(db, {"last_attempt_at": "garbage", "outcome": "COMPLETED"})
    assert hb.view(db)["outcome"] == "MALFORMED" and hb.view(db)["freshness"] == "UNAVAILABLE"
    _store(db, {"last_attempt_at": NOW_.replace(tzinfo=None).isoformat(), "outcome": "COMPLETED"})  # naive time
    assert hb.view(db)["outcome"] == "MALFORMED"
    _store(db, {"last_attempt_at": old, "last_completed_at": "x", "outcome": "STARTED", "duration_ms": True})
    v = hb.view(db)
    assert v["outcome"] == "UNFINISHED" and v["last_completed_at"] is None and v["duration_ms"] is None
    _store(db, "not a dict")
    assert hb.view(db)["outcome"] == "MALFORMED"


def test_watchdog_run_records_heartbeat_and_desk_exposes_it(monkeypatch):
    db = _db()
    monkeypatch.setattr(watchdog, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    result = watchdog.run({})
    assert result["business_tasks"] == []
    assert result["experiments"] is None                 # the scheduler is wired in and OFF by default
    assert hb.view(db)["outcome"] == "COMPLETED"
    state = desk.build_state(db, registry.load(), False)
    assert state["reconciler_execution"]["outcome"] == "COMPLETED"


# ---- PostgreSQL persistence ----------------------------------------------------------------------------

def test_postgres_persistence_across_sessions():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    try:
        with Session(engine) as a:
            hb.run(a, lambda d: ["TASK-1"])
        with Session(engine) as b:
            assert hb.view(b)["tasks_created"] == 1 and hb.view(b)["outcome"] == "COMPLETED"
            with pytest.raises(RuntimeError):
                hb.run(b, lambda d: (_ for _ in ()).throw(RuntimeError()))
        with Session(engine) as c:
            v = hb.view(c)
            assert v["outcome"] == "RAISED" and v["last_completed_at"]
            assert c.scalar(select(func.count()).select_from(models.ControlState)
                            .where(models.ControlState.key == hb.KEY)) == 1
    finally:
        models.Base.metadata.drop_all(engine)
        engine.dispose()


# ---- transaction isolation and overlapping attempts -----------------------------------------------------

def _file_db(tmp_path):
    """Two connections must be really separate, so use a file database (":memory:" shares one)."""
    engine = create_engine(f"sqlite:///{tmp_path}/hb.db")
    models.Base.metadata.create_all(engine)
    return engine


def _pending_note(db):
    db.add(models.Learning(title="pending caller work", lesson="l"))


def test_heartbeat_neither_commits_nor_rolls_back_unrelated_caller_work(tmp_path):
    engine = _file_db(tmp_path)
    caller = Session(engine)

    def reconcile(d):
        _pending_note(d)  # not flushed: SQLite allows one writer, so a flush would only test its lock
        return []
    hb.run(caller, reconcile)
    with Session(engine) as other:
        assert other.scalar(select(func.count()).select_from(models.Learning)) == 0  # heartbeat did not commit it
        assert hb.view(other)["outcome"] == "COMPLETED"
    assert caller.in_transaction()
    caller.commit()  # the caller's own commit is what persists it
    with Session(engine) as other:
        assert other.scalar(select(func.count()).select_from(models.Learning)) == 1


def test_failed_heartbeat_write_does_not_roll_back_caller_work(tmp_path, monkeypatch):
    engine = _file_db(tmp_path)
    caller = Session(engine)
    monkeypatch.setattr(hb, "_locked", lambda s: (_ for _ in ()).throw(OSError("db down")))

    def reconcile(d):
        _pending_note(d)
        return []
    assert hb.run(caller, reconcile) == []
    caller.commit()
    with Session(engine) as other:
        assert other.scalar(select(func.count()).select_from(models.Learning)) == 1


def test_raised_reconciler_keeps_caller_work_and_reraises(tmp_path):
    engine = _file_db(tmp_path)
    caller = Session(engine)

    def reconcile(d):
        _pending_note(d)
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        hb.run(caller, reconcile)
    with Session(engine) as other:
        assert hb.view(other)["outcome"] == "RAISED"


def test_older_attempt_cannot_overwrite_a_newer_one(tmp_path):
    engine = _file_db(tmp_path)
    older, newer = Session(engine), Session(engine)

    def slow(d):  # the older attempt is still running when a newer one starts and finishes
        hb.run(newer, lambda n: ["TASK-9"])
        return []
    hb.run(older, slow)
    with Session(engine) as r:
        v = hb.view(r)
        row = r.get(models.ControlState, hb.KEY).value
    assert v["tasks_created"] == 1 and row["created_task_keys"] == ["TASK-9"]  # the newer result survives
    assert v["outcome"] == "COMPLETED" and v["last_completed_at"]


def test_older_attempt_start_after_newer_does_not_replace_it(tmp_path):
    engine = _file_db(tmp_path)
    db = Session(engine)
    future = (hb._now() + timedelta(seconds=30)).isoformat()
    _store(db, {"last_attempt_at": future, "attempt_id": "newer", "outcome": "STARTED", "last_completed_at": None})
    hb.run(db, lambda d: ["TASK-1"])
    with Session(engine) as r:
        row = r.get(models.ControlState, hb.KEY).value
    assert row["attempt_id"] == "newer" and row["outcome"] == "STARTED"  # not overwritten, no invented success
    assert row["last_completed_at"]  # but the finished invocation's completion time is real and recorded


def test_future_dated_evidence_is_not_fresh():
    db = _db()
    future = (NOW_ + timedelta(hours=2)).isoformat()
    _store(db, {"last_attempt_at": future, "outcome": "COMPLETED"})
    v = hb.view(db)
    assert v["outcome"] == "MALFORMED" and v["freshness"] == "UNAVAILABLE" and v["last_attempt_at"] is None
    _store(db, {"last_attempt_at": NOW_.isoformat(), "last_completed_at": future, "outcome": "COMPLETED"})
    v = hb.view(db)  # COMPLETED whose only completion time is in the future is contradictory
    assert v["outcome"] == "MALFORMED" and v["freshness"] == "UNAVAILABLE" and v["last_completed_at"] is None
    _store(db, {"last_attempt_at": NOW_.isoformat(), "last_completed_at": future, "outcome": "RAISED"})
    v = hb.view(db)  # a future completion is dropped, never shown
    assert v["outcome"] == "RAISED" and v["last_completed_at"] is None
    _store(db, {"last_attempt_at": NOW_.isoformat(), "outcome": "weird"})
    assert hb.view(db)["freshness"] == "UNAVAILABLE"


US = timedelta(microseconds=1)
CLOCK = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("offset,future", [
    (-US, False), (timedelta(0), False), (US, True), (timedelta(minutes=4), True),
    (timedelta(minutes=5), True), (timedelta(minutes=6), True), (timedelta(hours=2), True)])
def test_attempt_boundary_against_the_evaluation_clock(offset, future):
    db = _db()
    at = (CLOCK + offset).isoformat()
    _store(db, {"last_attempt_at": at, "last_completed_at": at, "outcome": "COMPLETED"})
    v = hb.view(db, now=CLOCK)
    if future:
        assert (v["outcome"], v["freshness"], v["last_attempt_at"], v["last_completed_at"]) == (
            "MALFORMED", "UNAVAILABLE", None, None)
    else:
        assert (v["outcome"], v["freshness"]) == ("COMPLETED", "VERIFIED")
    assert v["freshness"] != "VERIFIED" or not future  # no future value is ever VERIFIED


@pytest.mark.parametrize("age,fresh", [(hb.FRESH_FOR, "VERIFIED"), (hb.FRESH_FOR + US, "STALE"), (timedelta(days=3), "STALE")])
def test_stale_boundary_is_unchanged(age, fresh):
    db = _db()
    at = (CLOCK - age).isoformat()
    _store(db, {"last_attempt_at": at, "last_completed_at": at, "outcome": "COMPLETED"})
    assert hb.view(db, now=CLOCK)["freshness"] == fresh


@pytest.mark.parametrize("value", ["2026-10-08T11:59:00", "garbage", "", None, 12345, "2026-13-45T00:00:00+00:00"])
def test_naive_and_malformed_attempt_timestamps_are_untrusted(value):
    db = _db()
    _store(db, {"last_attempt_at": value, "last_completed_at": (CLOCK - timedelta(minutes=1)).isoformat(),
                "outcome": "COMPLETED"})
    v = hb.view(db, now=CLOCK)
    assert (v["outcome"], v["freshness"], v["last_attempt_at"]) == ("MALFORMED", "UNAVAILABLE", None)


def test_future_started_and_raised_are_untrusted_too():
    db = _db()
    for outcome in ("STARTED", "RAISED"):
        _store(db, {"last_attempt_at": (CLOCK + timedelta(minutes=1)).isoformat(), "outcome": outcome})
        assert hb.view(db, now=CLOCK)["freshness"] == "UNAVAILABLE"


def _pg_engine():
    """Disposable PostgreSQL only; never a configured or production database."""
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=8)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def pg():
    engine = _pg_engine()
    yield engine
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def _learnings(engine):
    with Session(engine) as o:
        return o.scalar(select(func.count()).select_from(models.Learning))


def test_postgres_reconciler_exception_keeps_unrelated_caller_work(pg):
    caller = Session(pg)

    def boom(d):
        _pending_note(d)
        d.flush()
        raise RuntimeError("reconciler broke")
    with pytest.raises(RuntimeError, match="reconciler broke"):
        hb.run(caller, boom)
    assert _learnings(pg) == 0  # still uncommitted, and not rolled back either
    with Session(pg) as o:
        assert hb.view(o)["outcome"] == "RAISED"
    caller.commit()
    assert _learnings(pg) == 1  # the caller's work survived the exception and the heartbeat


def test_postgres_heartbeat_write_failure_keeps_unrelated_caller_work(pg, monkeypatch):
    caller = Session(pg)
    monkeypatch.setattr(hb, "_locked", lambda s: (_ for _ in ()).throw(OSError("db down")))
    assert hb.run(caller, lambda d: (_pending_note(d), d.flush(), ["TASK-1"])[2]) == ["TASK-1"]
    assert _learnings(pg) == 0
    caller.commit()
    assert _learnings(pg) == 1
    with Session(pg) as o:
        assert hb.view(o)["outcome"] == "NO_EVIDENCE"  # nothing invented


def test_postgres_uncommitted_caller_rows_stay_invisible_until_caller_commits(pg):
    caller = Session(pg)
    seen = []

    def reconcile(d):
        _pending_note(d)
        d.flush()
        seen.append(_learnings(pg))  # an independent session mid-run
        return []
    hb.run(caller, reconcile)
    seen.append(_learnings(pg))  # after the heartbeat's own commits
    caller.rollback()
    seen.append(_learnings(pg))  # the caller's rollback still wins
    assert seen == [0, 0, 0]


def test_postgres_concurrent_attempts_preserve_latest_accepted_evidence(pg):
    outer, inner = Session(pg), Session(pg)
    # `outer` starts first and finishes last; `inner` runs and finishes entirely in between.
    hb.run(outer, lambda d: (hb.run(inner, lambda n: ["TASK-NEW"]), ["TASK-OLD"])[1])
    with Session(pg) as o:
        row = o.get(models.ControlState, hb.KEY).value
        v = hb.view(o)
    assert row["created_task_keys"] == ["TASK-NEW"] and v["tasks_created"] == 1 and v["outcome"] == "COMPLETED"
    assert row["last_completed_at"] >= row["completed_at"]  # completion time only moves forward


def test_postgres_overlapping_attempts_and_isolation():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    engine = create_engine(url, pool_size=8)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    try:
        # 1. Unrelated pending work is neither committed nor lost.
        caller = Session(engine)
        hb.run(caller, lambda d: (_pending_note(d), d.flush(), [])[2])  # flushed, uncommitted
        with Session(engine) as o:
            assert o.scalar(select(func.count()).select_from(models.Learning)) == 0
        caller.commit()
        with Session(engine) as o:
            assert o.scalar(select(func.count()).select_from(models.Learning)) == 1
        # 2. Delayed completion: the older attempt finishes after the newer one.
        older, newer = Session(engine), Session(engine)
        hb.run(older, lambda d: (hb.run(newer, lambda n: ["TASK-NEW"]), ["TASK-OLD"])[1])
        with Session(engine) as o:
            row = o.get(models.ControlState, hb.KEY).value
            assert row["created_task_keys"] == ["TASK-NEW"] and row["outcome"] == "COMPLETED"
        # 3. Simultaneous first writes never lose the row, duplicate it or raise.
        with engine.begin() as c:
            c.execute(models.ControlState.__table__.delete())
        gate = Barrier(6)

        def one(i):
            with Session(engine) as s:
                gate.wait()
                return hb.run(s, lambda d: [f"TASK-{i}"])
        with ThreadPoolExecutor(6) as pool:
            assert sorted(sum(pool.map(one, range(6)), [])) == [f"TASK-{i}" for i in range(6)]
        with Session(engine) as o:
            assert o.scalar(select(func.count()).select_from(models.ControlState)
                            .where(models.ControlState.key == hb.KEY)) == 1
            v = hb.view(o)
            assert v["outcome"] in ("COMPLETED", "STARTED") and v["last_completed_at"]
    finally:
        models.Base.metadata.drop_all(engine)
        engine.dispose()


def _rows(db):
    return [db.scalar(select(func.count()).select_from(m)) for m in
            (models.Incident, models.Event, models.Task, models.Approval, models.AuditLog)]


def test_heartbeat_ticks_create_no_incidents_and_needs_you_does_not_accumulate(monkeypatch):
    """needs_you is recomputed from the one evidence row: repeated ticks never add alerts, rows or noise."""
    db = _db()
    _goal(db)
    before = _rows(db)
    for _ in range(3):  # healthy ticks: no reconciler item, no incident, no event
        hb.run(db, lambda d: [])
    state = desk.build_state(db, registry.load(), False)
    assert [i for i in state["needs_you"] if "reconciler" in i["title"].lower()] == []
    assert _rows(db) == before

    def bad(*a, **k):
        raise ValueError("goal broke")
    monkeypatch.setattr(br, "findings", bad)
    for _ in range(3):  # the same goal fails every tick: one item, refreshed, never repeated
        hb.run(db, br.reconcile)
    items = [i for i in desk.build_state(db, registry.load(), False)["needs_you"]
             if "reconciler" in i["title"].lower()]
    assert len(items) == 1 and items[0]["evidence"] == "1 goal error(s) logged"  # this tick, not cumulative
    assert _rows(db) == before  # visibility only: no incident, event, task, approval or audit row
