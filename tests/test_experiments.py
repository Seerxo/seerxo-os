"""Experiment evidence engine: exact series, deterministic difference, founder decisions, learning candidates."""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import enums, experiments as ex, models

FOUNDER = "founder:access:founder@example.com"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "28d", "source": "youtube-analytics-api",
       "unit": "count"}
ROOT = Path(__file__).resolve().parent.parent


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(models.BusinessUnit(key="youtube", name="AxiomTales", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1))
    db.commit()
    return db


def _obs(db, value, at, unit="count", source="youtube-analytics-api", metric="views"):
    unit_id = db.scalar(select(models.BusinessUnit.id))
    row = models.MetricObservation(business_unit_id=unit_id, subject="youtube:channel", metric=metric,
                                   window="28d", value=value, unit=unit, source=source, observed_at=at)
    db.add(row)
    db.commit()
    return row


def _propose(db, **kw):
    args = dict(title="Shorter intros", hypothesis="Shorter intros raise views", business_unit="youtube",
                metric_selector=SEL, expected_direction="UP", measurement_window_hours=48, actor="ceo",
                min_effect=100)
    args.update(kw)
    exp = ex.propose(db, **args)
    db.commit()
    return exp


def _running(db, **kw):
    _obs(db, 1000, T0 - timedelta(hours=1))
    exp = _propose(db, **kw)
    ex.approve_ready(db, exp, FOUNDER, now=T0)
    ex.start(db, exp, "TASK-0100", "developer", now=T0)
    db.commit()
    return exp


def _evaluated(db, after=1500, **kw):
    exp = _running(db, **kw)
    done = T0 + timedelta(hours=49)
    _obs(db, after, done)
    ex.close_window(db, exp, now=done)
    ex.evaluate(db, exp, now=done + timedelta(minutes=1))
    db.commit()
    return exp


def test_full_lifecycle_computes_an_observed_difference_not_causality():
    db = _db()
    exp = _evaluated(db)
    assert exp.status == enums.ExperimentStatus.EVALUATED and exp.outcome == "IMPROVED"
    assert exp.evidence["delta"] == 500 and exp.evidence["delta_pct"] == 50.0
    assert exp.evidence["claim"] == "OBSERVED_DIFFERENCE" and "NOT_ESTABLISHED" in exp.evidence["causality"]
    ex.decide(db, exp, True, "founder:access:admin@example.com", "keep it", now=T0 + timedelta(days=3))
    learning = ex.propose_learning(db, exp, "ceo")
    db.commit()
    assert exp.status == enums.ExperimentStatus.ACCEPTED
    assert exp.decided_by == "founder:access:admin@example.com" and exp.decision_reason == "keep it" and exp.decided_at
    assert learning.status == enums.LearningStatus.CANDIDATE and exp.key in learning.evidence
    assert ex.propose_learning(db, exp, "ceo").id == learning.id  # one candidate only
    actions = [a for a in db.scalars(select(models.AuditLog.action).order_by(models.AuditLog.id))]
    assert actions == ["experiment.proposed", "experiment.ready", "experiment.running",
                       "experiment.awaiting_measurement", "experiment.evaluated", "experiment.accepted",
                       "experiment.learning_candidate"]


@pytest.mark.parametrize("after,direction,outcome", [(1050, "UP", "NO_MEANINGFUL_CHANGE"), (800, "UP", "WORSENED"),
                                                     (800, "DOWN", "IMPROVED")])
def test_outcome_rules(after, direction, outcome):
    db = _db()
    _obs(db, 1000, T0 - timedelta(hours=1))
    exp = _propose(db, expected_direction=direction)
    ex.approve_ready(db, exp, FOUNDER, now=T0)
    ex.start(db, exp, "TASK-1", "dev", now=T0)
    _obs(db, after, T0 + timedelta(hours=49))
    ex.close_window(db, exp, now=T0 + timedelta(hours=49))
    assert ex.evaluate(db, exp, now=T0 + timedelta(hours=50)).outcome == outcome


def test_validation_of_the_proposal():
    db = _db()
    for kw in ({"metric_selector": {**SEL, "source": "made-up"}},  # wrong source
               {"metric_selector": {**SEL, "source": "polar-api"}},  # source does not report for this unit
               {"metric_selector": {k: v for k, v in SEL.items() if k != "unit"}},
               {"expected_direction": "SIDEWAYS"}, {"measurement_window_hours": 0}, {"business_unit": "nope"},
               {"goal_id": 99}):
        with pytest.raises(ex.ExperimentError):
            _propose(db, **kw)
    project = models.Project(key="axiomtales", name="A", business_unit_id=db.scalar(select(models.BusinessUnit.id)))
    db.add(project)
    db.flush()
    goal = models.Goal(title="g", metric="views", target="10", metric_selector={**SEL, "metric": "likes"},
                       project_id=project.id)
    db.add(goal)
    db.commit()
    with pytest.raises(ex.ExperimentError, match="different series"):
        _propose(db, goal_id=goal.id)


def test_missing_stale_or_wrong_unit_baseline_refuses():
    db = _db()
    exp = _propose(db)
    with pytest.raises(ex.ExperimentError, match="no baseline"):
        ex.approve_ready(db, exp, FOUNDER, now=T0)
    _obs(db, 5, T0 - timedelta(days=30))  # older than the source's freshness window
    with pytest.raises(ex.ExperimentError, match="stale"):
        ex.approve_ready(db, exp, FOUNDER, now=T0)
    _obs(db, 5, T0 - timedelta(hours=1), unit="seconds")
    with pytest.raises(ex.ExperimentError, match="unit"):
        ex.approve_ready(db, exp, FOUNDER, now=T0)
    _obs(db, 5, T0 - timedelta(hours=1), metric="likes", source="youtube-analytics-api")  # other series ignored
    assert exp.status == enums.ExperimentStatus.PROPOSED


def test_invalid_transitions_and_no_partial_mutation():
    db = _db()
    exp = _propose(db)
    for call in (lambda: ex.start(db, exp, "TASK-1", "dev", now=T0),
                 lambda: ex.close_window(db, exp, now=T0), lambda: ex.evaluate(db, exp, now=T0),
                 lambda: ex.decide(db, exp, True, FOUNDER, "x", now=T0)):
        with pytest.raises(ex.ExperimentError):
            call()
    assert exp.intervention_ref is None and exp.decided_by is None and exp.status == enums.ExperimentStatus.PROPOSED
    ex.cancel(db, exp, "ceo", "dropped")
    with pytest.raises(ex.ExperimentError):
        ex.approve_ready(db, exp, FOUNDER, now=T0)


def test_window_must_elapse_and_result_must_come_after_it():
    db = _db()
    exp = _running(db)
    with pytest.raises(ex.ExperimentError, match="window ends"):
        ex.close_window(db, exp, now=T0 + timedelta(hours=47))
    ex.close_window(db, exp, now=T0 + timedelta(hours=48))
    _obs(db, 9999, T0 + timedelta(hours=10))  # inside the window: not an outcome
    with pytest.raises(ex.ExperimentError, match="insufficient evidence"):
        ex.evaluate(db, exp, now=T0 + timedelta(hours=60))
    assert exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT and exp.outcome is None
    _obs(db, 1200, T0 + timedelta(hours=60), unit="seconds")
    with pytest.raises(ex.ExperimentError, match="unit"):
        ex.evaluate(db, exp, now=T0 + timedelta(hours=61))


def test_duplicate_processing_is_idempotent():
    db = _db()
    exp = _evaluated(db)
    first = dict(exp.evidence)
    n = db.scalar(select(func.count()).select_from(models.AuditLog))
    _obs(db, 1, T0 + timedelta(hours=70))  # a later reading must not rewrite the verdict
    assert ex.evaluate(db, exp, now=T0 + timedelta(hours=80)).evidence == first
    ex.decide(db, exp, False, FOUNDER, "no", now=T0 + timedelta(days=3))
    ex.decide(db, exp, False, FOUNDER, "no", now=T0 + timedelta(days=4))
    assert db.scalar(select(func.count()).select_from(models.AuditLog)) == n + 1
    with pytest.raises(ex.ExperimentError):  # a decided experiment cannot be flipped
        ex.decide(db, exp, True, FOUNDER, "changed my mind")


def test_unauthorized_actors_cannot_approve_or_decide():
    db = _db()
    _obs(db, 1000, T0 - timedelta(hours=1))
    exp = _propose(db)
    for who in ("ceo", "company-worker", "", None, "founderish", "system:experiments"):
        with pytest.raises(PermissionError):
            ex.approve_ready(db, exp, who, now=T0)
    ex.approve_ready(db, exp, FOUNDER, now=T0)
    ex.start(db, exp, "TASK-1", "dev", now=T0)
    _obs(db, 1500, T0 + timedelta(hours=49))
    ex.close_window(db, exp, now=T0 + timedelta(hours=49))
    ex.evaluate(db, exp, now=T0 + timedelta(hours=50))
    for who in ("ceo", "system:experiments", None):
        with pytest.raises(PermissionError):
            ex.decide(db, exp, True, who, "x")
    with pytest.raises(ex.ExperimentError, match="reason"):
        ex.decide(db, exp, True, FOUNDER, " ")
    with pytest.raises(ex.ExperimentError, match="only a decided"):
        ex.propose_learning(db, exp, "ceo")
    assert exp.status == enums.ExperimentStatus.EVALUATED and exp.learning_id is None


def test_lifecycle_never_touches_goals_tasks_or_approved_learnings():
    db = _db()
    project = models.Project(key="axiomtales", name="A", business_unit_id=db.scalar(select(models.BusinessUnit.id)))
    db.add(project)
    db.flush()
    goal = models.Goal(title="g", metric="views", target="10", status=enums.GoalStatus.ACTIVE, project_id=project.id,
                       metric_selector={k: v for k, v in SEL.items() if k != "unit"})
    db.add(goal)
    db.commit()
    snap = (goal.status, goal.target, goal.baseline)
    exp = _evaluated(db, goal_id=goal.id)
    ex.decide(db, exp, True, FOUNDER, "ok", now=T0 + timedelta(days=3))
    ex.propose_learning(db, exp, "ceo")
    db.commit()
    assert (goal.status, goal.target, goal.baseline) == snap
    assert db.scalar(select(func.count()).select_from(models.Task)) == 0
    assert db.scalar(select(func.count()).select_from(models.Learning)
                     .where(models.Learning.status == enums.LearningStatus.APPROVED)) == 0


def test_view_computes_freshness_from_timestamps():
    db = _db()
    exp = _evaluated(db)
    assert ex.view(exp, now=T0 + timedelta(days=3))["result_freshness"] == "VERIFIED"
    assert ex.view(exp, now=T0 + timedelta(days=30))["result_freshness"] == "STALE"
    assert not hasattr(models.Experiment, "is_fresh")


# ---- migration: PostgreSQL, disposable database only ----------------------------------------------------

def test_migration_upgrades_and_downgrades_cleanly():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    models.Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(text("drop table if exists alembic_version"))
        c.execute(text("drop type if exists experiment_status"))
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True,
                              capture_output=True, text=True)
    def alembic_fails(*args, **extra):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env={**env, **extra},
                              capture_output=True, text=True)
    try:
        alembic("upgrade", "head")
        cols = {c["name"] for c in inspect(engine).get_columns("experiments")}
        assert {"key", "metric_selector", "baseline_observation_id", "decided_by", "learning_id",
                "goal_selector_snapshot", "fingerprint"} <= cols
        with engine.connect() as c:   # constraints and the partial unique index exist in the migrated schema
            names = {r[0] for r in c.execute(text("select conname from pg_constraint where conrelid = 'experiments'::regclass"))}
            assert {"ck_experiments_direction", "ck_experiments_min_effect", "ck_experiments_window_hours"} <= names
            index = c.execute(text("select indexdef from pg_indexes where indexname = 'uq_experiments_open_fingerprint'")).scalar()
            assert "UNIQUE" in index and "PROPOSED" in index and "EVALUATED" in index
        with Session(engine) as s:  # the migrated schema works with the model
            s.add(models.BusinessUnit(key="youtube", name="Y", status=enums.BusinessUnitStatus.ACTIVE,
                                      priority=enums.Priority.P1))
            s.commit()
            _propose(s)
        # DOWNGRADE DESTROYS EXPERIMENT ROWS: it refuses while any exist, and changes nothing.
        refused = alembic_fails("downgrade", "c7d2e8a4f1b3")
        assert refused.returncode != 0 and "refusing to drop experiments" in refused.stderr
        assert "experiments" in inspect(engine).get_table_names()
        with engine.connect() as c:
            assert c.execute(text("select count(*) from experiments")).scalar() == 1
        confirmed = alembic_fails("downgrade", "c7d2e8a4f1b3", EXPERIMENTS_DOWNGRADE_CONFIRM_DATA_LOSS="yes")
        assert confirmed.returncode == 0, confirmed.stderr
        assert "experiments" not in inspect(engine).get_table_names()
        with engine.connect() as c:
            assert c.execute(text("select count(*) from pg_type where typname='experiment_status'")).scalar() == 0
        assert "learnings" in inspect(engine).get_table_names()  # nothing else was touched
        alembic("upgrade", "head")
        assert "experiments" in inspect(engine).get_table_names()
        alembic("downgrade", "c7d2e8a4f1b3")   # an empty table needs no confirmation
        alembic("upgrade", "head")
        # Other migrations may follow experiments; prove the disposable database reached today's one head.
        assert alembic("current").stdout.split()[0] == alembic("heads").stdout.split()[0]
    finally:
        models.Base.metadata.drop_all(engine)
        with engine.begin() as c:
            c.execute(text("drop table if exists alembic_version"))
            c.execute(text("drop type if exists experiment_status"))
        engine.dispose()
