"""Experiment scheduler tick: closes elapsed windows, evaluates valid evidence, nothing else."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app import audit, enums, experiment_scheduler as sched, experiments as ex, metrics_store, models

FOUNDER = "founder:access:founder@example.com"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "28d", "source": "youtube-analytics-api",
       "unit": "count"}
END = T0 + timedelta(hours=48)   # measure_after of an experiment started at T0 with a 48h window
ROOT = Path(__file__).resolve().parent.parent
REAL = T0 + timedelta(days=365)   # the fixed "real" clock: every fixed test time is in its past


@pytest.fixture(autouse=True)
def _real_clock(monkeypatch):
    """Tests pin the real clock the same way they inject `now`; production has no such switch."""
    monkeypatch.setattr(sched, "_real_now", lambda: REAL)


def _db(url="sqlite://"):
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi, _):   # SQLAlchemy's documented pysqlite fix: SAVEPOINT and rollback then work
        dbapi.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(models.BusinessUnit(key="youtube", name="AxiomTales", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1))
    db.commit()
    return db


def _obs(db, value, at, unit="count", source="youtube-analytics-api", metric="views"):
    row = models.MetricObservation(business_unit_id=db.scalar(select(models.BusinessUnit.id)),
                                   subject="youtube:channel", metric=metric, window="28d", value=value, unit=unit,
                                   source=source, observed_at=at)
    db.add(row)
    db.commit()
    return row


def _running(db, n=0):
    """A RUNNING experiment started at T0 (distinct hypothesis per n, so each is its own experiment)."""
    if not db.scalar(select(models.MetricObservation.id)):
        _obs(db, 1000, T0 - timedelta(hours=1))
    exp = ex.propose(db, title="Shorter intros", hypothesis=f"Shorter intros raise views {n}", business_unit="youtube",
                     metric_selector=SEL, expected_direction="UP", measurement_window_hours=48, actor="ceo",
                     min_effect=100, now=T0)
    ex.approve_ready(db, exp, FOUNDER, now=T0)
    ex.start(db, exp, f"TASK-{n:04d}", "developer", now=T0)
    db.commit()
    return exp


def _audits(db, action=None):
    q = select(func.count()).select_from(models.AuditLog).where(models.AuditLog.entity_type == "experiment")
    return db.scalar(q.where(models.AuditLog.action == action) if action else q)


def _cats(result):
    return [(o.key, o.category, o.reason) for o in result.outcomes]


# --- windows ---------------------------------------------------------------------------------
def test_a_window_that_has_not_elapsed_is_left_alone():
    db = _db()
    exp = _running(db)
    before = _audits(db)
    for now in (T0 + timedelta(hours=1), END - timedelta(seconds=1)):
        result = sched.tick(db, now)
        assert result.outcomes == [] and result.transitions == 0
    assert exp.status == enums.ExperimentStatus.RUNNING and _audits(db) == before


def test_an_elapsed_window_closes_and_waits_for_data_without_inventing_any():
    db = _db()
    exp = _running(db)
    rows = db.scalar(select(func.count()).select_from(models.MetricObservation))
    result = sched.tick(db, END)   # exactly at the end counts as elapsed
    assert _cats(result) == [(exp.key, "WINDOW_CLOSED", None), (exp.key, "WAITING_FOR_DATA", "NO_POST_WINDOW_OBSERVATION")]
    assert exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT and exp.outcome is None and exp.evidence is None
    assert db.scalar(select(func.count()).select_from(models.MetricObservation)) == rows


def test_a_correct_post_window_observation_is_evaluated_with_full_provenance():
    db = _db()
    exp = _running(db)
    _obs(db, 1500, END + timedelta(hours=1))
    result = sched.tick(db, END + timedelta(hours=2))
    assert [o.category for o in result.outcomes] == ["WINDOW_CLOSED", "EVALUATED"]
    d = result.outcomes[1].detail
    base, res = db.get(models.MetricObservation, exp.baseline_observation_id), db.get(models.MetricObservation, exp.result_observation_id)
    assert (d["outcome"], d["baseline_observation_id"], d["result_observation_id"]) == ("IMPROVED", base.id, res.id)
    assert (d["source"], d["unit"], d["window_hours"], d["experiment"]) == ("youtube-analytics-api", "count", 48, exp.key)
    assert d["evaluated_at"] == (END + timedelta(hours=2)).isoformat() and d["delta"] == 500
    assert d["causality"].startswith("NOT_ESTABLISHED") and d["statistically_proven"] is False
    assert not {"p_value", "significance", "confidence"} & set(d)
    assert exp.status == enums.ExperimentStatus.EVALUATED and exp.evidence["claim"] == "OBSERVED_DIFFERENCE"


def test_missing_data_stays_waiting_even_when_overdue_and_is_never_a_verdict():
    db = _db()
    exp = _running(db)
    sched.tick(db, END)
    late = END + metrics_store.stale_after("youtube-analytics-api") + timedelta(days=3)
    result = sched.tick(db, late)
    [o] = result.outcomes
    assert (o.category, o.reason) == ("WAITING_FOR_DATA", "NO_POST_WINDOW_OBSERVATION") and o.detail["overdue"] is True
    assert exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT and exp.outcome is None


def test_observations_of_other_series_are_not_data_for_this_experiment():
    db = _db()
    exp = _running(db)
    _obs(db, 99, END + timedelta(hours=1), metric="likes")
    _obs(db, 99, END + timedelta(hours=1), source="youtube-data-api")
    result = sched.tick(db, END + timedelta(hours=2))
    assert result.outcomes[-1].category == "WAITING_FOR_DATA" and exp.outcome is None


def test_a_first_reading_that_came_too_late_is_permanently_insufficient():
    db = _db()
    exp = _running(db)
    gap = metrics_store.stale_after("youtube-analytics-api") + timedelta(hours=1)
    _obs(db, 1500, END + gap)
    result = sched.tick(db, END + gap + timedelta(hours=1))
    assert result.outcomes[-1].category == "INSUFFICIENT_EVIDENCE" and result.outcomes[-1].reason == "LATE_FIRST_OBSERVATION"
    assert result.outcomes[-1].detail["permanent"] is True and exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT


def test_a_wrong_unit_or_a_foreign_baseline_is_insufficient_not_waiting():
    db = _db()
    wrong_unit = _running(db, 1)
    _obs(db, 1500, END + timedelta(hours=1), unit="ratio")
    r = sched.tick(db, END + timedelta(hours=2))
    assert (r.outcomes[-1].category, r.outcomes[-1].reason) == ("INSUFFICIENT_EVIDENCE", "UNIT_MISMATCH")
    assert wrong_unit.outcome is None
    db2 = _db()
    foreign = _running(db2, 2)
    other = _obs(db2, 7, T0 - timedelta(minutes=5), metric="likes")
    foreign.baseline_observation_id = other.id
    db2.commit()
    _obs(db2, 1500, END + timedelta(hours=1))
    r = sched.tick(db2, END + timedelta(hours=2))
    assert (r.outcomes[-1].category, r.outcomes[-1].reason) == ("INSUFFICIENT_EVIDENCE", "SERIES_MISMATCH")


# --- repetition, isolation, recovery ---------------------------------------------------------
def test_repeating_a_tick_is_idempotent_and_writes_no_second_audit_row():
    db = _db()
    exp = _running(db)
    _obs(db, 1500, END + timedelta(hours=1))
    first = sched.tick(db, END + timedelta(hours=2), commit=True)
    audits, snapshot = _audits(db), (exp.status, exp.outcome, dict(exp.evidence))
    for _ in range(3):
        again = sched.tick(db, END + timedelta(hours=3), commit=True)
        assert again.outcomes == [] and again.transitions == 0
    assert (exp.status, exp.outcome, exp.evidence) == snapshot and _audits(db) == audits
    assert _audits(db, "experiment.awaiting_measurement") == 1 and _audits(db, "experiment.evaluated") == 1
    assert first.transitions == 2


def test_waiting_experiments_report_the_same_state_each_tick_without_writing():
    db = _db()
    _running(db)
    sched.tick(db, END, commit=True)
    audits = _audits(db)
    a, b = sched.tick(db, END + timedelta(hours=1)), sched.tick(db, END + timedelta(hours=2))
    assert [o.fingerprint for o in a.outcomes] == [o.fingerprint for o in b.outcomes] and len(a.outcomes) == 1
    assert _audits(db) == audits
    assert db.scalar(select(func.count()).select_from(models.Task)) == 0   # no task, incident or notification


def test_one_broken_experiment_never_blocks_the_healthy_ones():
    db = _db()
    healthy, broken, bad_link = _running(db, 1), _running(db, 2), _running(db, 3)
    other = _obs(db, 7, T0 - timedelta(minutes=5), metric="likes")
    bad_link.baseline_observation_id = other.id
    db.commit()
    _obs(db, 1500, END + timedelta(hours=1))
    real = ex.close_window

    def close(db_, exp, *a, **k):
        if exp.key == broken.key:
            raise RuntimeError("disk on fire")
        return real(db_, exp, *a, **k)
    ex.close_window = close
    try:
        result = sched.tick(db, END + timedelta(hours=2))
    finally:
        ex.close_window = real
    by_key = {}
    for o in result.outcomes:
        by_key.setdefault(o.key, []).append(o.category)
    assert by_key[healthy.key] == ["WINDOW_CLOSED", "EVALUATED"]
    assert by_key[broken.key] == ["ERROR"] and by_key[bad_link.key] == ["WINDOW_CLOSED", "INSUFFICIENT_EVIDENCE"]
    assert broken.status == enums.ExperimentStatus.RUNNING   # its failed attempt left nothing behind
    err = [o for o in result.outcomes if o.category == "ERROR"][0]
    assert err.detail == {"error": "RuntimeError"}   # type only: no message, no value


def test_a_failed_transition_inside_the_savepoint_leaves_no_partial_state(monkeypatch):
    db = _db()
    exp = _running(db)
    monkeypatch.setattr(audit, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    result = sched.tick(db, END)
    assert [o.category for o in result.outcomes] == ["ERROR"]
    db.rollback()
    db.refresh(exp)
    assert exp.status == enums.ExperimentStatus.RUNNING and exp.measure_after is not None


class Crash(BaseException):
    """A process-level interruption, not an Exception: nothing in the tick may swallow it."""


def test_restart_after_an_interruption_finishes_the_work_without_repeating_it(monkeypatch):
    db = _db()
    first, second = _running(db, 1), _running(db, 2)
    _obs(db, 1500, END + timedelta(hours=1))
    real = ex.close_window

    def close(db_, exp, *a, **k):
        if exp.key == second.key:
            raise Crash()
        return real(db_, exp, *a, **k)
    monkeypatch.setattr(ex, "close_window", close)
    with pytest.raises(Crash):
        sched.tick(db, END + timedelta(hours=2), commit=True)
    db.rollback()
    assert first.status == enums.ExperimentStatus.EVALUATED          # committed before the crash
    assert second.status == enums.ExperimentStatus.RUNNING
    monkeypatch.setattr(ex, "close_window", real)
    result = sched.tick(db, END + timedelta(hours=3), commit=True)  # the restart
    assert [(o.key, o.category) for o in result.outcomes] == [(second.key, "WINDOW_CLOSED"), (second.key, "EVALUATED")]
    assert _audits(db, "experiment.evaluated") == 2 and _audits(db, "experiment.awaiting_measurement") == 2


def test_an_experiment_closed_but_not_evaluated_resumes_at_evaluation():
    db = _db()
    exp = _running(db)
    ex.close_window(db, exp, "system:experiments", now=END)   # the previous process stopped right here
    db.commit()
    _obs(db, 1500, END + timedelta(hours=1))
    result = sched.tick(db, END + timedelta(hours=2))
    assert [o.category for o in result.outcomes] == ["EVALUATED"] and exp.status == enums.ExperimentStatus.EVALUATED


def test_without_commit_the_caller_owns_the_transaction():
    db = _db()
    exp = _running(db)
    sched.tick(db, END)
    assert exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT
    db.rollback()
    db.refresh(exp)
    assert exp.status == enums.ExperimentStatus.RUNNING
    sched.tick(db, END, commit=True)
    db.rollback()
    db.refresh(exp)
    assert exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT


# --- bounds and modes ------------------------------------------------------------------------
def test_batches_are_bounded_oldest_window_first_and_resume_on_the_next_tick():
    db = _db()
    exps = [_running(db, n) for n in range(5)]
    _obs(db, 1500, END + timedelta(hours=1))
    first = sched.tick(db, END + timedelta(hours=2), limit=2, commit=True)
    assert first.advanced == 2 and first.transitions == 4 and first.truncated is True
    assert sum(e.status == enums.ExperimentStatus.RUNNING for e in exps) == 3
    sched.tick(db, END + timedelta(hours=2), limit=2, commit=True)
    last = sched.tick(db, END + timedelta(hours=2), limit=2, commit=True)
    assert all(e.status == enums.ExperimentStatus.EVALUATED for e in exps) and last.truncated is False
    with pytest.raises(ValueError):
        sched.tick(db, END, limit=0)


def test_dry_run_reports_what_would_happen_and_persists_nothing():
    db = _db()
    exp = _running(db)
    _obs(db, 1500, END + timedelta(hours=1))
    audits = _audits(db)
    result = sched.tick(db, END + timedelta(hours=2), dry_run=True, commit=True)
    assert result.dry_run is True and [o.category for o in result.outcomes] == ["WINDOW_CLOSED", "EVALUATED"]
    db.refresh(exp)
    assert exp.status == enums.ExperimentStatus.RUNNING and exp.evidence is None and _audits(db) == audits


def test_time_is_utc_safe_and_naive_time_is_read_as_utc():
    db = _db()
    exp = _running(db)
    plus3 = timezone(timedelta(hours=3))
    assert sched.tick(db, (END - timedelta(seconds=1)).astimezone(plus3), dry_run=True).outcomes == []
    assert sched.tick(db, END.astimezone(plus3), dry_run=True).outcomes[0].category == "WINDOW_CLOSED"
    assert sched.tick(db, END.replace(tzinfo=None), dry_run=True).outcomes[0].category == "WINDOW_CLOSED"
    assert exp.status == enums.ExperimentStatus.RUNNING


def test_the_result_is_json_serializable_and_summarized():
    db = _db()
    _running(db)
    _obs(db, 1500, END + timedelta(hours=1))
    d = sched.tick(db, END + timedelta(hours=2)).to_dict()
    assert json.loads(json.dumps(d)) == d and d["counts"] == {"WINDOW_CLOSED": 1, "EVALUATED": 1}
    assert d["transitions"] == 2 and d["advanced"] == 1 and d["dry_run"] is False


def test_the_scheduler_is_off_unless_switched_on_and_touches_nothing_when_off(monkeypatch):
    monkeypatch.delenv("EXPERIMENT_SCHEDULER_ENABLED", raising=False)
    assert sched.enabled() is False
    db = _db()
    exp = _running(db)
    assert sched.run_if_enabled(db, END) is None and exp.status == enums.ExperimentStatus.RUNNING
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_ENABLED", "true")
    monkeypatch.delenv("EXPERIMENT_SCHEDULER_APPLY", raising=False)
    assert sched.enabled() is True and sched.run_if_enabled(db, END) is None   # one switch is not enough
    assert exp.status == enums.ExperimentStatus.RUNNING
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_APPLY", "yes")
    assert sched.apply_enabled() is True and sched.run_if_enabled(db, END).advanced == 1


def test_the_command_line_is_a_dry_run_unless_doubly_confirmed(monkeypatch, capsys, tmp_path):
    db = _db(f"sqlite:///{tmp_path}/scheduler.db")   # a file: the command opens its own session and connection
    exp = _running(db)
    key = exp.key

    def status():
        value = db.scalar(select(models.Experiment.status).where(models.Experiment.key == key))
        db.commit()   # end this session's read transaction so the command's own connection can write
        return value

    def factory():
        return Session(db.get_bind())
    monkeypatch.delenv("EXPERIMENT_SCHEDULER_APPLY", raising=False)
    monkeypatch.delenv("EXPERIMENT_SCHEDULER_ENABLED", raising=False)
    db.commit()
    assert sched.main(["--now", END.isoformat()], session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True and status() == enums.ExperimentStatus.RUNNING
    assert sched.main(["--apply", "--now", END.isoformat()], session_factory=factory) == 2   # env confirmation missing
    assert status() == enums.ExperimentStatus.RUNNING
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_APPLY", "yes")
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_ENABLED", "true")
    assert sched.main(["--apply", "--now", END.isoformat()], session_factory=factory) == 0
    assert status() == enums.ExperimentStatus.AWAITING_MEASUREMENT


# --- authority and side effects --------------------------------------------------------------
def test_founder_only_and_other_steps_are_never_executed(monkeypatch):
    db = _db()
    proposed = ex.propose(db, title="Other idea", hypothesis="Another idea entirely", business_unit="youtube",
                          metric_selector=SEL, expected_direction="UP", measurement_window_hours=48, actor="ceo", now=T0)
    _obs(db, 1000, T0 - timedelta(hours=1))
    ready = ex.propose(db, title="Third idea", hypothesis="A third idea entirely", business_unit="youtube",
                       metric_selector=SEL, expected_direction="UP", measurement_window_hours=48, actor="ceo", now=T0)
    ex.approve_ready(db, ready, FOUNDER, now=T0)
    running = _running(db, 9)
    evaluated = _running(db, 8)
    ex.close_window(db, evaluated, now=END)
    _obs(db, 1500, END + timedelta(hours=1))
    ex.evaluate(db, evaluated, now=END + timedelta(hours=2))
    db.commit()

    def forbidden(*a, **k):
        raise AssertionError("the scheduler must not call this")
    for name in ("propose", "approve_ready", "start", "decide", "cancel", "propose_learning"):
        monkeypatch.setattr(ex, name, forbidden)
    sched.tick(db, END + timedelta(hours=3), commit=True)
    assert (proposed.status, ready.status) == (enums.ExperimentStatus.PROPOSED, enums.ExperimentStatus.READY)
    assert evaluated.status == enums.ExperimentStatus.EVALUATED and evaluated.decided_by is None
    assert running.status == enums.ExperimentStatus.EVALUATED and running.decided_by is None
    source = (ROOT / "app" / "experiment_scheduler.py").read_text()
    for name in ("approve_ready", "decide(", "propose_learning", "founder:access", "LearningStatus", "approval_identity"):
        assert name not in source


def test_a_tick_has_no_side_effects_beyond_the_experiments_it_advances():
    db = _db()
    project = models.Project(key="axiomtales", name="A", business_unit_id=db.scalar(select(models.BusinessUnit.id)))
    db.add(project)
    db.flush()
    goal = models.Goal(title="g", metric="views", target="10", status=enums.GoalStatus.ACTIVE, project_id=project.id,
                       metric_selector={k: v for k, v in SEL.items() if k != "unit"})
    db.add(goal)
    db.commit()
    _obs(db, 1000, T0 - timedelta(hours=1))
    exp = ex.propose(db, title="Goal linked", hypothesis="Goal linked idea", business_unit="youtube", metric_selector=SEL,
                     expected_direction="UP", measurement_window_hours=48, actor="ceo", goal_id=goal.id, now=T0)
    ex.approve_ready(db, exp, FOUNDER, now=T0)
    ex.start(db, exp, "TASK-1", "dev", now=T0)
    db.commit()
    goal.metric_selector = {"subject": "youtube:channel", "metric": "likes", "window": "28d"}   # repointed meanwhile
    db.commit()
    _obs(db, 1500, END + timedelta(hours=1))
    tables = (models.Task, models.Approval, models.Goal, models.Learning, models.MetricObservation, models.Incident, models.Event)
    before = [db.scalar(select(func.count()).select_from(t)) for t in tables]
    goal_state = (goal.status, goal.target, dict(goal.metric_selector))
    sched.tick(db, END + timedelta(hours=2), commit=True)
    assert [db.scalar(select(func.count()).select_from(t)) for t in tables] == before
    assert (goal.status, goal.target, goal.metric_selector) == goal_state
    assert exp.status == enums.ExperimentStatus.EVALUATED and exp.evidence["goal_link"]["status"] == "GOAL_SELECTOR_CHANGED"
    assert exp.decided_by is None and exp.learning_id is None   # prepared for the founder, not decided


# --- corrective: enablement gate, clock guard, fair scanning ------------------------------------
def _snapshot(db):
    db.expire_all()
    rows = [(e.key, e.status, e.outcome, e.evaluated_at) for e in db.scalars(select(models.Experiment)
                                                                              .order_by(models.Experiment.id))]
    return rows, _audits(db), db.scalar(select(func.count()).select_from(models.ControlState))


@pytest.mark.parametrize("enabled, apply", [
    (None, None), (None, "yes"), ("true", None), ("true", "no"), ("false", "yes"), ("yes", "yes"), ("1", "yes"),
    ("true", "YES"),
])
def test_apply_and_run_if_enabled_need_both_switches_and_otherwise_change_nothing(monkeypatch, enabled, apply):
    for name, value in (("EXPERIMENT_SCHEDULER_ENABLED", enabled), ("EXPERIMENT_SCHEDULER_APPLY", apply)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    db = _db()
    _running(db)
    before = _snapshot(db)

    def factory():
        raise AssertionError("a refused --apply must not open a session")
    assert sched.main(["--apply", "--now", END.isoformat()], session_factory=factory) == 2
    assert sched.apply_enabled() is False
    statements = []
    event.listen(db.get_bind(), "before_cursor_execute", lambda *a: statements.append(a[2]))
    assert sched.run_if_enabled(db, END) is None and statements == []   # not even a read
    assert _snapshot(db) == before


def test_dry_run_command_needs_no_switch_and_writes_nothing(monkeypatch, capsys):
    monkeypatch.delenv("EXPERIMENT_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("EXPERIMENT_SCHEDULER_APPLY", raising=False)
    db = _db()
    _running(db)
    _obs(db, 1500, END + timedelta(hours=1))
    before = _snapshot(db)
    monkeypatch.setattr(db, "close", lambda: None)
    assert sched.main(["--now", (END + timedelta(hours=2)).isoformat()], session_factory=lambda: db) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {"WINDOW_CLOSED": 1, "EVALUATED": 1}
    assert _snapshot(db) == before   # including no cursor row


@pytest.mark.parametrize("commit", [False, True])
def test_a_future_clock_is_refused_before_any_read_or_write(monkeypatch, commit):
    db = _db()
    _running(db)
    before = _snapshot(db)
    statements = []
    event.listen(db.get_bind(), "before_cursor_execute", lambda *a: statements.append(a[2]))
    ahead = REAL + sched.CLOCK_SKEW + timedelta(seconds=1)
    with pytest.raises(sched.FutureClock):
        sched.tick(db, ahead, commit=commit)
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_APPLY", "yes")
    with pytest.raises(sched.FutureClock):
        sched.run_if_enabled(db, ahead)
    assert statements == []   # rejected before the database was touched
    assert _snapshot(db) == before


def test_the_clock_tolerance_is_exact_and_a_past_clock_is_allowed():
    db = _db()
    exp = _running(db)
    with pytest.raises(sched.FutureClock):
        sched.tick(db, REAL + sched.CLOCK_SKEW + timedelta(microseconds=1))
    assert sched.tick(db, REAL + sched.CLOCK_SKEW).outcomes   # within tolerance: allowed
    db.rollback()
    assert sched.tick(db, END).outcomes[0].category == "WINDOW_CLOSED"   # the past cannot close a window early
    db.rollback()
    db.refresh(exp)
    assert exp.status == enums.ExperimentStatus.RUNNING


def test_within_the_tolerance_a_window_can_close_up_to_clock_skew_early(monkeypatch):
    """The accepted limit, documented rather than denied: real clock 4 minutes before the window ends."""
    monkeypatch.setattr(sched, "_real_now", lambda: END - timedelta(minutes=4))
    db = _db()
    exp = _running(db)
    assert [o.category for o in sched.tick(db, END, commit=True).outcomes] == ["WINDOW_CLOSED", "WAITING_FOR_DATA"]
    assert exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT
    with pytest.raises(sched.FutureClock):   # but not one microsecond beyond the tolerance
        sched.tick(db, END - timedelta(minutes=4) + sched.CLOCK_SKEW + timedelta(microseconds=1), commit=True)


def test_a_dry_run_may_simulate_the_future_and_keeps_nothing():
    db = _db()
    _running(db)
    before = _snapshot(db)
    result = sched.tick(db, REAL + timedelta(days=30), dry_run=True, commit=True)
    assert result.outcomes[0].category == "WINDOW_CLOSED"
    assert _snapshot(db) == before


def test_the_command_refuses_a_future_now_before_opening_a_session(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_APPLY", "yes")

    def factory():
        raise AssertionError("must not open a session")
    ahead = (REAL + timedelta(days=30)).isoformat()
    assert sched.main(["--apply", "--now", ahead], session_factory=factory) == 2


def _stuck_awaiting(db, n):
    """n experiments whose first post-window reading is in the wrong unit: permanently INSUFFICIENT_EVIDENCE."""
    exps = [_running(db, i) for i in range(n)]
    _obs(db, 2000, END + timedelta(minutes=5), unit="usd")
    for _ in range(n):   # bounded: each tick closes at least one more window
        sched.tick(db, END + timedelta(hours=1), commit=True, limit=n)
    db.expire_all()
    assert all(e.status == enums.ExperimentStatus.AWAITING_MEASUREMENT for e in exps)
    return exps


def _fresh(db, n):
    start = T0 + timedelta(hours=10)
    exp = ex.propose(db, title="Fresh", hypothesis=f"fresh {n}", business_unit="youtube", metric_selector=SEL,
                     expected_direction="UP", measurement_window_hours=48, actor="ceo", now=start)
    if n == 0:
        _obs(db, 1001, start - timedelta(minutes=1))   # a fresh baseline shared by every fresh experiment
    ex.approve_ready(db, exp, FOUNDER, now=start)
    ex.start(db, exp, f"TASK-9{n:03d}", "developer", now=start)
    db.commit()
    return exp


def test_stuck_experiments_beyond_the_scan_cap_cannot_hide_newer_work(monkeypatch):
    monkeypatch.setattr(sched, "SCAN_CAP", 3)
    db = _db()
    _stuck_awaiting(db, 7)                 # more stuck rows than the cap, all older (lower ids)
    fresh = [_fresh(db, n) for n in range(2)]
    later = T0 + timedelta(hours=70)
    for _ in range(4):                      # ceil(9 / 3) = 3 ticks suffice; one spare
        sched.tick(db, later, commit=True)
    db.expire_all()
    assert all(e.status == enums.ExperimentStatus.AWAITING_MEASUREMENT for e in fresh)   # each window closed
    rows = [_snapshot(db)[1]]
    for _ in range(3):
        sched.tick(db, later, commit=True)
    assert _snapshot(db)[1] == rows[0]   # once everything has been seen, repeated laps write no audit rows


def test_without_advances_one_scheduler_examines_every_candidate_within_ceil_n_over_cap_ticks(monkeypatch):
    """Nothing here can advance, so the limit never stops a tick: min(limit, SCAN_CAP) = SCAN_CAP."""
    monkeypatch.setattr(sched, "SCAN_CAP", 4)
    db = _db()
    stuck = _stuck_awaiting(db, 10)
    seen = set()
    for _ in range(3):   # ceil(10 / 4)
        seen |= {o.key for o in sched.tick(db, END + timedelta(hours=2), commit=True).outcomes}
    assert seen == {e.key for e in stuck}


@pytest.mark.parametrize("value", [None, {"after_id": -3}, {"after_id": "7"}, {"after_id": True}, "garbage",
                                   {"after_id": 10 ** 9}, {"after_id": 2 ** 31}, {"after_id": 10 ** 20}])
def test_a_missing_or_corrupt_cursor_only_restarts_the_rotation(value):
    db = _db()
    exp = _running(db)
    if value is not None:
        db.add(models.ControlState(key=sched.CURSOR_KEY, value=value, updated_by="t"))
        db.commit()
    assert sched.tick(db, END, commit=True).outcomes[0].category == "WINDOW_CLOSED"
    assert db.get(models.ControlState, sched.CURSOR_KEY).value == {"after_id": exp.id}


def test_a_dry_run_does_not_move_the_cursor():
    db = _db()
    _running(db)
    sched.tick(db, END, dry_run=True, commit=True)
    assert db.get(models.ControlState, sched.CURSOR_KEY) is None


def _waiting(db, n, start=0):
    """n RUNNING experiments past their window with no post-window reading: each closes once, then waits forever."""
    return [_running(db, start + i) for i in range(n)]


def _examined(monkeypatch):
    seen, real = {}, sched._process
    tick_no = [0]

    def spy(db, exp_id, *a, **k):
        seen.setdefault(exp_id, tick_no[0])
        return real(db, exp_id, *a, **k)
    monkeypatch.setattr(sched, "_process", spy)
    return seen, tick_no


def test_the_advancement_limit_slows_the_rotation_but_one_scheduler_still_reaches_every_candidate(monkeypatch):
    """N=30, SCAN_CAP=10, limit=2: ceil(N / SCAN_CAP) = 3 ticks is NOT enough (the old, wrong claim); the supported
    bound for one scheduler is ceil(N / min(limit, SCAN_CAP)) = 15, and the cursor passes >= 2 candidates a tick."""
    monkeypatch.setattr(sched, "SCAN_CAP", 10)
    db = _db()
    ids = [e.id for e in _waiting(db, 30)]
    seen, tick_no = _examined(monkeypatch)
    cursors = []
    for tick_no[0] in range(1, 16):
        sched.tick(db, END, limit=2, commit=True)
        cursors.append(db.get(models.ControlState, sched.CURSOR_KEY).value["after_id"])
        if tick_no[0] == 3:
            assert not set(ids) <= set(seen)   # the advancement limit stopped every tick early
    assert set(seen) == set(ids) and max(seen.values()) == 15
    assert cursors == [ids[i] for i in range(1, 30, 2)]   # forward by exactly limit=2 while every visit advances
    db.expire_all()
    assert {e.status for e in db.scalars(select(models.Experiment))} == {enums.ExperimentStatus.AWAITING_MEASUREMENT}
    seen.clear()
    tick_no[0] = 16
    sched.tick(db, END, limit=2, commit=True)
    assert len(seen) == 10   # nothing left to advance: a tick now passes SCAN_CAP waiting candidates


def test_candidates_added_between_ticks_are_reached_and_do_not_starve_older_ones(monkeypatch):
    monkeypatch.setattr(sched, "SCAN_CAP", 10)
    db = _db()
    old = [e.id for e in _waiting(db, 30)]
    seen, tick_no = _examined(monkeypatch)
    new = []
    for tick_no[0] in range(1, 19):   # ceil(35 / min(2, 10)) = 18, counting every candidate that existed
        if tick_no[0] == 5:
            new = [e.id for e in _waiting(db, 5, start=100)]
        sched.tick(db, END, limit=2, commit=True)
    assert set(old) | set(new) <= set(seen)


def test_a_commit_false_tick_never_creates_the_cursor_and_so_cannot_rotate_on_its_own(monkeypatch):
    """Fairness needs a persisted cursor: a caller that never commits a tick of its own gets no rotation."""
    monkeypatch.setattr(sched, "SCAN_CAP", 2)
    db = _db()
    _stuck_awaiting(db, 4)
    db.query(models.ControlState).delete()
    db.commit()
    keys = [{o.key for o in sched.tick(db, END + timedelta(hours=2), commit=False).outcomes} for _ in range(3)]
    db.commit()
    assert db.get(models.ControlState, sched.CURSOR_KEY) is None
    assert keys[0] == keys[1] == keys[2] and len(keys[0]) == 2   # the same first SCAN_CAP every time
    sched.tick(db, END + timedelta(hours=2), commit=True)        # one self-committing tick creates it
    nxt = {o.key for o in sched.tick(db, END + timedelta(hours=2), commit=False).outcomes}
    assert nxt.isdisjoint(keys[0])                                # from then on commit=False ticks rotate too


# --- periodic wiring: the watchdog tick (docs/EXPERIMENT_SCHEDULER.md, "Integration point") -----------------
def _watchdog_on(monkeypatch, db, enabled=None, apply=None):
    from app import watchdog
    for name, value in (("EXPERIMENT_SCHEDULER_ENABLED", enabled), ("EXPERIMENT_SCHEDULER_APPLY", apply)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(watchdog, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    return watchdog


@pytest.mark.parametrize("enabled,apply", [(None, None), ("true", None), (None, "yes"), ("false", "yes"), ("true", "no")])
def test_the_watchdog_tick_leaves_experiments_alone_unless_both_switches_are_set(monkeypatch, enabled, apply):
    db = _db()
    exp = _running(db)                                   # its window is long over by the pinned real clock
    before = _audits(db)
    watchdog = _watchdog_on(monkeypatch, db, enabled, apply)
    assert watchdog.run_experiment_scheduler() is None
    db.refresh(exp)
    assert exp.status == enums.ExperimentStatus.RUNNING and _audits(db) == before


def test_the_watchdog_tick_advances_an_elapsed_window_when_the_founder_enabled_both(monkeypatch):
    db = _db()
    exp = _running(db)
    watchdog = _watchdog_on(monkeypatch, db, "true", "yes")
    out = watchdog.run_experiment_scheduler()
    db.refresh(exp)
    assert out["counts"].get("WINDOW_CLOSED", 0) + out["counts"].get("WAITING_FOR_DATA", 0) >= 1, out
    assert exp.status != enums.ExperimentStatus.RUNNING and exp.decided_by is None   # never a founder decision
    assert watchdog.run_experiment_scheduler()["counts"].get("WINDOW_CLOSED", 0) == 0   # idempotent next tick


def test_a_scheduler_failure_never_breaks_the_watchdog_tick(monkeypatch):
    db = _db()
    watchdog = _watchdog_on(monkeypatch, db, "true", "yes")
    def boom(_db):
        raise RuntimeError("secret-bearing message must not be reported")
    monkeypatch.setattr(sched, "run_if_enabled", boom)
    assert watchdog.run_experiment_scheduler() == {"error": "RuntimeError"}   # type only, never the message


def test_compose_passes_both_switches_to_the_api_and_defaults_them_off():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "EXPERIMENT_SCHEDULER_ENABLED: ${EXPERIMENT_SCHEDULER_ENABLED:-false}" in compose
    assert "EXPERIMENT_SCHEDULER_APPLY: ${EXPERIMENT_SCHEDULER_APPLY:-}" in compose
