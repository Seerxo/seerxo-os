"""The experiment scheduler against real PostgreSQL: SKIP LOCKED claims, savepoint isolation, crash recovery.

Same guard as the other PostgreSQL suites: only the disposable CI database is allowed.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Thread

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import enums, experiment_scheduler as sched, experiments as ex, models

FOUNDER = "founder:access:founder@example.com"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
END = T0 + timedelta(hours=48)
LATER = END + timedelta(hours=2)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "28d", "source": "youtube-analytics-api",
       "unit": "count"}
N = 6
REAL = T0 + timedelta(days=365)


@pytest.fixture(autouse=True)
def _real_clock(monkeypatch):
    monkeypatch.setattr(sched, "_real_now", lambda: REAL)


@pytest.fixture()
def engine():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=10)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.BusinessUnit(key="youtube", name="Y", status=enums.BusinessUnitStatus.ACTIVE,
                                   priority=enums.Priority.P1))
        db.commit()
        unit = db.scalar(select(models.BusinessUnit.id))
        for value, at in ((1000, T0 - timedelta(hours=1)), (1500, END + timedelta(hours=1))):   # baseline, result
            db.add(models.MetricObservation(business_unit_id=unit, subject="youtube:channel", metric="views",
                                            window="28d", value=value, unit="count", source="youtube-analytics-api",
                                            observed_at=at))
        db.commit()
    yield engine
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def _running(engine, n=N) -> list[str]:
    keys = []
    with Session(engine) as db:
        for i in range(n):
            exp = ex.propose(db, title="Shorter intros", hypothesis=f"Shorter intros raise views {i}",
                             business_unit="youtube", metric_selector=SEL, expected_direction="UP",
                             measurement_window_hours=48, actor="ceo", min_effect=100, now=T0)
            ex.approve_ready(db, exp, FOUNDER, now=T0)
            ex.start(db, exp, f"TASK-{i}", "dev", now=T0)
            keys.append(exp.key)
        db.commit()
    return keys


def _audits(engine, action):
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.entity_type == "experiment", models.AuditLog.action == action))


def _status(engine, key):
    with Session(engine) as db:
        return db.scalar(select(models.Experiment.status).where(models.Experiment.key == key))


def _race(engine, jobs):
    gate = Barrier(len(jobs))

    def run(job):
        with Session(engine) as db:
            gate.wait()
            return job(db)
    with ThreadPoolExecutor(len(jobs)) as pool:
        return list(pool.map(run, jobs))


@pytest.fixture()
def slow_close(monkeypatch):
    """Hold each experiment's row lock for a moment, so a second scheduler would collide with it."""
    real = ex.close_window

    def slow(db, exp, *a, **k):
        out = real(db, exp, *a, **k)
        time.sleep(0.15)
        return out
    monkeypatch.setattr(ex, "close_window", slow)


def test_concurrent_schedulers_advance_each_experiment_exactly_once(engine, slow_close):
    keys = _running(engine)
    results = _race(engine, [lambda db: sched.tick(db, LATER, commit=True)] * 3)
    closed = [o.key for r in results for o in r.outcomes if o.category == "WINDOW_CLOSED"]
    evaluated = [o.key for r in results for o in r.outcomes if o.category == "EVALUATED"]
    assert sorted(closed) == sorted(keys) and sorted(evaluated) == sorted(keys)   # once each, across all schedulers
    assert any(o.category == "SKIPPED" for r in results for o in r.outcomes)      # the losers skipped, not waited
    assert _audits(engine, "experiment.awaiting_measurement") == N and _audits(engine, "experiment.evaluated") == N
    assert all(_status(engine, k) == enums.ExperimentStatus.EVALUATED for k in keys)


def test_a_repeated_tick_after_the_work_is_done_changes_nothing(engine):
    keys = _running(engine, 2)
    with Session(engine) as db:
        sched.tick(db, LATER, commit=True)
    before = (_audits(engine, "experiment.awaiting_measurement"), _audits(engine, "experiment.evaluated"))
    for _ in range(3):
        with Session(engine) as db:
            r = sched.tick(db, LATER + timedelta(hours=5), commit=True)
            assert r.outcomes == [] and r.transitions == 0
    assert before == (2, 2) == (_audits(engine, "experiment.awaiting_measurement"), _audits(engine, "experiment.evaluated"))
    assert all(_status(engine, k) == enums.ExperimentStatus.EVALUATED for k in keys)


def test_a_database_error_in_one_experiment_rolls_back_only_that_savepoint(engine, monkeypatch):
    keys = _running(engine, 3)
    real = ex.close_window

    def close(db, exp, *a, **k):
        if exp.key == keys[1]:
            db.execute(text("select 1 / 0"))   # a genuine PostgreSQL error: aborts the transaction without a savepoint
        return real(db, exp, *a, **k)
    monkeypatch.setattr(ex, "close_window", close)
    with Session(engine) as db:
        result = sched.tick(db, LATER, commit=True)
    by_key = {}
    for o in result.outcomes:
        by_key.setdefault(o.key, []).append(o.category)
    assert by_key[keys[1]] == ["ERROR"] and by_key[keys[0]] == by_key[keys[2]] == ["WINDOW_CLOSED", "EVALUATED"]
    assert _status(engine, keys[1]) == enums.ExperimentStatus.RUNNING
    assert _audits(engine, "experiment.evaluated") == 2
    monkeypatch.setattr(ex, "close_window", real)   # the broken one heals on the next tick, once
    with Session(engine) as db:
        again = sched.tick(db, LATER, commit=True)
    assert [(o.key, o.category) for o in again.outcomes] == [(keys[1], "WINDOW_CLOSED"), (keys[1], "EVALUATED")]
    assert _audits(engine, "experiment.evaluated") == 3


def test_a_database_error_does_not_poison_the_callers_single_transaction(engine, monkeypatch):
    """commit=False: one caller transaction spans every experiment, so only the savepoint keeps a PostgreSQL error
    in one of them from aborting the work of all the others."""
    keys = _running(engine, 3)
    real = ex.close_window

    def close(db, exp, *a, **k):
        if exp.key == keys[1]:
            db.execute(text("select 1 / 0"))
        return real(db, exp, *a, **k)
    monkeypatch.setattr(ex, "close_window", close)
    with Session(engine) as db:
        result = sched.tick(db, LATER)
        db.commit()   # the caller's one commit
    assert [o.category for o in result.outcomes if o.key == keys[1]] == ["ERROR"]
    assert [_status(engine, k) for k in keys] == [enums.ExperimentStatus.EVALUATED, enums.ExperimentStatus.RUNNING,
                                                 enums.ExperimentStatus.EVALUATED]


def test_without_commit_the_callers_transaction_decides(engine):
    [key] = _running(engine, 1)
    with Session(engine) as db:
        sched.tick(db, LATER)               # caller owns the transaction ...
        db.rollback()                       # ... and abandons it
    assert _status(engine, key) == enums.ExperimentStatus.RUNNING and _audits(engine, "experiment.evaluated") == 0
    with Session(engine) as db:
        sched.tick(db, LATER)
        db.commit()
    assert _status(engine, key) == enums.ExperimentStatus.EVALUATED


class Crash(BaseException):
    pass


def test_a_crash_midway_keeps_finished_work_and_the_restart_completes_the_rest(engine, monkeypatch):
    keys = _running(engine, 3)
    real = ex.close_window

    def close(db, exp, *a, **k):
        if exp.key == keys[2]:
            raise Crash()
        return real(db, exp, *a, **k)
    monkeypatch.setattr(ex, "close_window", close)
    with Session(engine) as db:
        with pytest.raises(Crash):
            sched.tick(db, LATER, commit=True)
        db.rollback()
    assert [_status(engine, k) for k in keys] == [enums.ExperimentStatus.EVALUATED] * 2 + [enums.ExperimentStatus.RUNNING]
    with Session(engine) as db:   # the crashed process held a row lock; its connection is gone, so the lock is too
        assert db.scalar(select(models.Experiment.id).where(models.Experiment.key == keys[2]).with_for_update(skip_locked=True))
    monkeypatch.setattr(ex, "close_window", real)
    with Session(engine) as db:
        result = sched.tick(db, LATER, commit=True)
    assert [(o.key, o.category) for o in result.outcomes] == [(keys[2], "WINDOW_CLOSED"), (keys[2], "EVALUATED")]
    assert _audits(engine, "experiment.evaluated") == 3 and _audits(engine, "experiment.awaiting_measurement") == 3


def test_a_cancel_racing_the_scheduler_leaves_one_consistent_state(engine, monkeypatch):
    [key] = _running(engine, 1)
    claimed = Event()
    release = Event()
    cancel_pid_ready = Event()
    cancel_pid = {}
    real_close = ex.close_window

    def hold_claimed_row(db, exp, *args, **kwargs):
        claimed.set()  # _process already holds the row lock before close_window is called.
        if not release.wait(timeout=15):
            raise TimeoutError("test did not release the scheduler")
        return real_close(db, exp, *args, **kwargs)
    monkeypatch.setattr(ex, "close_window", hold_claimed_row)

    def tick():
        with Session(engine) as db:
            return sched.tick(db, LATER, commit=True)

    def cancel():
        with Session(engine) as db:
            try:
                with db.begin():
                    cancel_pid["value"] = db.scalar(text("select pg_backend_pid()"))
                    cancel_pid_ready.set()
                    ex.cancel(db, db.scalar(select(models.Experiment).where(models.Experiment.key == key)),
                              "ceo", "changed")
                return "cancelled"
            except ex.ExperimentError:
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        ticking = pool.submit(tick)
        cancelling = None
        try:
            assert claimed.wait(timeout=5), "scheduler never claimed the experiment row"
            cancelling = pool.submit(cancel)
            assert cancel_pid_ready.wait(timeout=5), "cancel worker never opened its PostgreSQL session"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with engine.connect() as observer:
                    waiting = observer.scalar(text("""
                        select wait_event_type = 'Lock' from pg_stat_activity where pid = :pid
                    """), {"pid": cancel_pid["value"]})
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting, "cancel worker never waited on the scheduler's row lock"
        finally:
            release.set()
        tick_result = ticking.result(timeout=10)
        cancelled = cancelling.result(timeout=10)

    assert tick_result.advanced == 1
    status = _status(engine, key)
    assert status == enums.ExperimentStatus.EVALUATED and cancelled == "refused"
    assert _audits(engine, "experiment.cancelled") == 0


def test_timestamps_compare_correctly_across_time_zones_in_postgres(engine):
    keys = _running(engine, 1)
    plus3 = timezone(timedelta(hours=3))
    with Session(engine) as db:
        assert sched.tick(db, (END - timedelta(seconds=1)).astimezone(plus3), commit=True).outcomes == []
    assert _status(engine, keys[0]) == enums.ExperimentStatus.RUNNING
    with Session(engine) as db:
        assert sched.tick(db, END.astimezone(plus3), commit=True).outcomes[0].category == "WINDOW_CLOSED"


def test_the_scheduler_touches_no_other_table(engine):
    _running(engine, 2)
    tables = (models.Task, models.Approval, models.Goal, models.Learning, models.MetricObservation, models.Incident)
    with Session(engine) as db:
        before = [db.scalar(select(func.count()).select_from(t)) for t in tables]
        sched.tick(db, LATER, commit=True)
        assert [db.scalar(select(func.count()).select_from(t)) for t in tables] == before
        # the one write outside experiments/audit: its own advisory cursor row
        assert [r.key for r in db.scalars(select(models.ControlState))] == [sched.CURSOR_KEY]


# --- corrective: fair scanning beyond the cap, with concurrent schedulers --------------------------
def _stuck_and_fresh(engine, stuck, fresh):
    """`stuck` permanently insufficient experiments (lower ids), then `fresh` RUNNING ones with valid data."""
    with Session(engine) as db:
        unit = db.scalar(select(models.BusinessUnit.id))
        db.add(models.MetricObservation(business_unit_id=unit, subject="youtube:channel", metric="views",
                                        window="28d", value=9, unit="usd", source="youtube-analytics-api",
                                        observed_at=END - timedelta(minutes=30)))   # wrong unit, first after window
        db.commit()
    keys = _running(engine, stuck)
    with Session(engine) as db:
        db.execute(text("UPDATE experiments SET measure_after = :m WHERE status = 'RUNNING'"),
                   {"m": END - timedelta(hours=1)})   # their first post-window reading is the wrong-unit one
        db.commit()
        for _ in range(stuck):   # bounded: each tick closes at least one more window
            sched.tick(db, LATER, commit=True, limit=stuck)
    with Session(engine) as db:
        assert {_status(engine, k) for k in keys} == {"AWAITING_MEASUREMENT"}
        fresh_keys = []
        for i in range(fresh):
            exp = ex.propose(db, title="Fresh", hypothesis=f"fresh {i}", business_unit="youtube",
                             metric_selector=SEL, expected_direction="UP", measurement_window_hours=48,
                             actor="ceo", min_effect=100, now=T0)
            ex.approve_ready(db, exp, FOUNDER, now=T0)
            ex.start(db, exp, f"TASK-8{i:03d}", "developer", now=T0)
            fresh_keys.append(exp.key)
        db.commit()
    return keys, fresh_keys


def test_progress_beyond_the_scan_cap_with_concurrent_schedulers(engine, monkeypatch):
    monkeypatch.setattr(sched, "SCAN_CAP", 3)
    stuck, fresh = _stuck_and_fresh(engine, 8, 4)
    barrier = Barrier(3)

    def run(_):
        barrier.wait()
        with Session(engine) as db:
            return [sched.tick(db, LATER, commit=True) for _ in range(5)]
    with ThreadPoolExecutor(3) as pool:
        results = [r for rs in pool.map(run, range(3)) for r in rs]
    assert {_status(engine, k) for k in fresh} == {"EVALUATED"}   # none hidden behind the 8 stuck rows
    assert {_status(engine, k) for k in stuck} == {"AWAITING_MEASUREMENT"}
    # exactly once: one close and one evaluation audit row per fresh experiment, across all schedulers
    with Session(engine) as db:
        for action in ("experiment.awaiting_measurement", "experiment.evaluated"):
            n = db.scalar(select(func.count()).select_from(models.AuditLog).where(
                models.AuditLog.action == action, models.AuditLog.entity_key.in_(fresh)))
            assert n == len(fresh), action
    assert sum(o.category == sched.EVALUATED for r in results for o in r.outcomes) == len(fresh)


def test_a_cursor_row_held_by_another_scheduler_never_blocks_a_tick(engine):
    _running(engine, 1)
    with Session(engine) as db:
        sched.tick(db, LATER, commit=True)   # creates the cursor row
        before = db.get(models.ControlState, sched.CURSOR_KEY).value
    new = []                               # fresh work, so the next tick does want to move the cursor
    with Session(engine) as db:
        for i in range(2):
            exp = ex.propose(db, title="Late", hypothesis=f"late {i}", business_unit="youtube", metric_selector=SEL,
                             expected_direction="UP", measurement_window_hours=48, actor="ceo", min_effect=100,
                             now=T0)
            ex.approve_ready(db, exp, FOUNDER, now=T0)
            ex.start(db, exp, f"TASK-7{i:03d}", "developer", now=T0)
            new.append(exp.key)
        db.commit()
    holder = Session(engine)
    holder.scalar(select(models.ControlState).where(models.ControlState.key == sched.CURSOR_KEY).with_for_update())
    out = []

    def run():
        with Session(engine) as db:
            out.append(sched.tick(db, LATER, commit=True))   # its cursor update is skipped, not awaited
    th = Thread(target=run, daemon=True)
    try:
        th.start()
        th.join(5)
        assert not th.is_alive(), "the tick waited on another scheduler's cursor lock"
        [result] = out
        assert {o.key for o in result.outcomes} >= set(new)
        assert {_status(engine, k) for k in new} == {"EVALUATED"}   # the experiment work itself was committed
    finally:
        holder.rollback()
        holder.close()
    with Session(engine) as db:
        assert db.get(models.ControlState, sched.CURSOR_KEY).value == before   # the holder "won" this round


def test_a_future_clock_is_refused_on_postgres_before_any_statement(engine):
    _running(engine, 1)
    with Session(engine) as db:
        before = _audits(engine, "experiment.awaiting_measurement")
        with pytest.raises(sched.FutureClock):
            sched.tick(db, REAL + timedelta(hours=1), commit=False)
        db.commit()
    assert _audits(engine, "experiment.awaiting_measurement") == before


def _cursor_value(engine):
    with Session(engine) as db:
        row = db.get(models.ControlState, sched.CURSOR_KEY)
        return None if row is None else row.value


def _ids(engine, keys):
    with Session(engine) as db:
        return [db.scalar(select(models.Experiment.id).where(models.Experiment.key == k)) for k in keys]


def test_the_first_cursor_insert_never_waits_on_another_sessions_open_transaction(engine):
    """Session A ticks with commit=False and leaves its transaction open; session B's committing tick must not
    wait for A to end. An uncommitted INSERT of the cursor key by A would make B's INSERT of it wait."""
    keys = _running(engine, 3)
    holder = Session(engine)
    out = []

    def run():
        with Session(engine) as db:
            out.append(sched.tick(db, LATER, commit=True))
    th = Thread(target=run, daemon=True)
    try:
        assert sched.tick(holder, LATER, commit=False, limit=2).advanced == 2   # A holds keys[0..1], stays open
        th.start()
        th.join(5)
        assert not th.is_alive(), "cursor initialisation waited on another session's open transaction"
        [result] = out
        assert [o.key for o in result.outcomes if o.category == "SKIPPED"] == [f"id:{i}" for i in _ids(engine, keys[:2])]
        assert {o.key for o in result.outcomes if o.category == "EVALUATED"} == {keys[2]}
        assert _cursor_value(engine) == {"after_id": _ids(engine, keys)[2]}   # B created it and committed
    finally:
        holder.rollback()
        holder.close()
        th.join(10)
    assert [_status(engine, k) for k in keys] == [enums.ExperimentStatus.RUNNING] * 2 + [enums.ExperimentStatus.EVALUATED]


def test_a_commit_false_tick_moves_an_existing_cursor_only_inside_the_callers_transaction(engine):
    keys = _running(engine, 3)
    ids = _ids(engine, keys)
    with Session(engine) as db:
        sched.tick(db, LATER, commit=True, limit=1)
    assert _cursor_value(engine) == {"after_id": ids[0]}
    with Session(engine) as db:
        assert sched.tick(db, LATER, commit=False, limit=1).advanced == 1
        assert db.get(models.ControlState, sched.CURSOR_KEY).value == {"after_id": ids[1]}
        db.rollback()   # the caller's rollback takes the cursor move with it, like the experiment work
    assert _cursor_value(engine) == {"after_id": ids[0]}
    assert [_status(engine, k) for k in keys] == [enums.ExperimentStatus.EVALUATED] + [enums.ExperimentStatus.RUNNING] * 2
    with Session(engine) as db:
        sched.tick(db, LATER, commit=False, limit=1)
        db.commit()
    assert _cursor_value(engine) == {"after_id": ids[1]}


def test_a_crash_before_the_cursor_is_saved_keeps_the_work_and_only_repeats_idempotent_reads(engine, monkeypatch):
    keys = _running(engine, 3)
    ids = _ids(engine, keys)
    with Session(engine) as db:
        sched.tick(db, LATER, commit=True, limit=1)
    real = sched._save_cursor

    def crash(*a, **k):
        raise Crash()
    monkeypatch.setattr(sched, "_save_cursor", crash)
    with Session(engine) as db:
        with pytest.raises(Crash):
            sched.tick(db, LATER, commit=True, limit=1)
        db.rollback()
    assert _cursor_value(engine) == {"after_id": ids[0]}   # not saved
    assert _status(engine, keys[1]) == enums.ExperimentStatus.EVALUATED   # the experiment work was committed
    monkeypatch.setattr(sched, "_save_cursor", real)
    with Session(engine) as db:
        result = sched.tick(db, LATER, commit=True)
    assert [(o.key, o.category) for o in result.outcomes] == [(keys[2], "WINDOW_CLOSED"), (keys[2], "EVALUATED")]
    assert _audits(engine, "experiment.evaluated") == 3 and _audits(engine, "experiment.awaiting_measurement") == 3
    assert _cursor_value(engine) == {"after_id": ids[2]}


def test_three_schedulers_with_the_cap_and_the_limit_close_each_window_once_and_reach_every_candidate(engine,
                                                                                                    monkeypatch):
    """No tick bound is claimed under concurrency (the last cursor save wins, so it can move back); only exactly-once
    transitions and eventual progress."""
    monkeypatch.setattr(sched, "SCAN_CAP", 5)
    with Session(engine) as db:   # no post-window reading: every experiment waits for data once its window closes
        db.execute(text("DELETE FROM metric_observations WHERE observed_at > :t"), {"t": T0})
        db.commit()
    keys = _running(engine, 24)
    examined, real, errors = set(), ex.evaluate, []

    def spy(db, exp, *a, **k):
        examined.add(exp.key)
        return real(db, exp, *a, **k)
    monkeypatch.setattr(ex, "evaluate", spy)
    gate = Barrier(3)

    def run():
        try:
            with Session(engine) as db:
                gate.wait()
                for _ in range(15):
                    sched.tick(db, LATER, limit=2, commit=True)
        except Exception as e:   # noqa: BLE001 - reported below
            errors.append(repr(e))
    threads = [Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and not any(t.is_alive() for t in threads)
    assert examined == set(keys)
    assert _audits(engine, "experiment.awaiting_measurement") == len(keys)   # each window closed exactly once
    assert {_status(engine, k) for k in keys} == {enums.ExperimentStatus.AWAITING_MEASUREMENT}


def test_a_cursor_beyond_the_integer_id_range_only_restarts_the_rotation_on_postgres(engine):
    [key] = _running(engine, 1)
    with Session(engine) as db:
        db.add(models.ControlState(key=sched.CURSOR_KEY, value={"after_id": 10 ** 20}, updated_by="t"))
        db.commit()
        assert [o.category for o in sched.tick(db, LATER, commit=True).outcomes] == ["WINDOW_CLOSED", "EVALUATED"]
    assert _cursor_value(engine) == {"after_id": _ids(engine, [key])[0]}
