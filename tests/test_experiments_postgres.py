"""Experiment decisions under real PostgreSQL concurrency: row locks, one winner, one audit row.

Same guard as the other PostgreSQL suites: only the disposable CI database is allowed.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import enums, experiments as ex, models

FOUNDER = "founder:access:founder@example.com"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "28d", "source": "youtube-analytics-api",
       "unit": "count"}
WRITERS = 4


@pytest.fixture()
def engine():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=WRITERS + 2)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.BusinessUnit(key="youtube", name="Y", status=enums.BusinessUnitStatus.ACTIVE,
                                   priority=enums.Priority.P1))
        db.commit()
        unit = db.scalar(select(models.BusinessUnit.id))
        db.add(models.MetricObservation(business_unit_id=unit, subject="youtube:channel", metric="views",
                                        window="28d", value=1000, unit="count", source="youtube-analytics-api",
                                        observed_at=T0 - timedelta(hours=1)))
        db.commit()
    yield engine
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def _propose(db, **kw):
    args = dict(title="Shorter intros", hypothesis="Shorter intros raise views", business_unit="youtube",
                metric_selector=SEL, expected_direction="UP", measurement_window_hours=48, actor="ceo",
                min_effect=100, now=T0)
    exp = ex.propose(db, **(args | kw))
    db.commit()
    return exp


def _evaluated(engine) -> str:
    with Session(engine) as db:
        exp = _propose(db)
        ex.approve_ready(db, exp, FOUNDER, now=T0)
        ex.start(db, exp, "TASK-1", "dev", now=T0)
        ex.close_window(db, exp, now=T0 + timedelta(hours=49))
        db.add(models.MetricObservation(business_unit_id=exp.business_unit_id, subject="youtube:channel",
                                        metric="views", window="28d", value=1500, unit="count",
                                        source="youtube-analytics-api", observed_at=T0 + timedelta(hours=50)))
        ex.evaluate(db, exp, now=T0 + timedelta(hours=51))
        db.commit()
        return exp.key


def _race(engine, jobs):
    """Run every job at the same instant in its own session; returns [(ok, error)] in job order."""
    gate = Barrier(len(jobs))

    def run(job):
        with Session(engine) as db:
            gate.wait()
            try:
                job(db)
                db.commit()
                return True, None
            except (ex.ExperimentError, ex.NotFounder) as e:
                db.rollback()
                return False, e
    with ThreadPoolExecutor(len(jobs)) as pool:
        return list(pool.map(run, jobs))


@pytest.fixture()
def slow_check(monkeypatch):
    """Hold every transition between its status check and its write, so an unlocked row would race."""
    real = ex._allow

    def slow(exp, to):
        real(exp, to)
        time.sleep(0.25)
    monkeypatch.setattr(ex, "_allow", slow)


def _get(db, key):
    return db.scalar(select(models.Experiment).where(models.Experiment.key == key))


def _count(db, action):
    return db.scalar(select(func.count()).select_from(models.AuditLog).where(
        models.AuditLog.entity_type == "experiment", models.AuditLog.action == action))


def test_conflicting_decisions_have_exactly_one_winner(engine, slow_check):
    key = _evaluated(engine)
    jobs = [lambda db: ex.decide(db, _get(db, key), True, "founder:access:a@example.com", "ship", now=T0),
            lambda db: ex.decide(db, _get(db, key), False, "founder:access:b@example.com", "drop", now=T0)] * 2
    results = _race(engine, jobs)
    assert sum(ok for ok, _ in results) == 2 and sum(not ok for ok, _ in results) == 2   # winners' identical retries pass
    with Session(engine) as db:
        exp = _get(db, key)
        assert exp.status in (enums.ExperimentStatus.ACCEPTED, enums.ExperimentStatus.REJECTED)
        winner = "experiment.accepted" if exp.status == enums.ExperimentStatus.ACCEPTED else "experiment.rejected"
        loser = "experiment.rejected" if winner == "experiment.accepted" else "experiment.accepted"
        assert _count(db, winner) == 1 and _count(db, loser) == 0
        assert exp.decided_by in ("founder:access:a@example.com", "founder:access:b@example.com")


def test_duplicate_identical_decisions_are_one_decision(engine, slow_check):
    key = _evaluated(engine)
    results = _race(engine, [lambda db: ex.decide(db, _get(db, key), True, FOUNDER, "ship", now=T0)] * WRITERS)
    assert all(ok for ok, _ in results)
    with Session(engine) as db:
        assert _count(db, "experiment.accepted") == 1 and _get(db, key).status == enums.ExperimentStatus.ACCEPTED


def test_concurrent_identical_proposals_create_one_experiment(engine):
    results = _race(engine, [lambda db: _propose(db)] * WRITERS)
    assert all(ok for ok, _ in results), results
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(models.Experiment)) == 1
        assert _count(db, "experiment.proposed") == 1


def test_concurrent_evaluations_write_once(engine, slow_check):
    with Session(engine) as db:
        exp = _propose(db)
        ex.approve_ready(db, exp, FOUNDER, now=T0)
        ex.start(db, exp, "TASK-1", "dev", now=T0)
        ex.close_window(db, exp, now=T0 + timedelta(hours=49))
        db.add(models.MetricObservation(business_unit_id=exp.business_unit_id, subject="youtube:channel",
                                        metric="views", window="28d", value=1500, unit="count",
                                        source="youtube-analytics-api", observed_at=T0 + timedelta(hours=50)))
        db.commit()
        key = exp.key
    now = T0 + timedelta(hours=51)
    results = _race(engine, [lambda db: ex.evaluate(db, _get(db, key), now=now)] * WRITERS)
    assert all(ok for ok, _ in results)
    with Session(engine) as db:
        assert _count(db, "experiment.evaluated") == 1 and _get(db, key).outcome == "IMPROVED"


def test_approval_racing_a_cancel_never_leaves_a_half_state(engine, slow_check):
    with Session(engine) as db:
        key = _propose(db).key
    results = _race(engine, [lambda db: ex.approve_ready(db, _get(db, key), FOUNDER, now=T0),
                             lambda db: ex.cancel(db, _get(db, key), "ceo", "changed my mind")])
    assert any(ok for ok, _ in results)
    with Session(engine) as db:
        exp = _get(db, key)
        assert exp.status in (enums.ExperimentStatus.READY, enums.ExperimentStatus.CANCELLED)
        assert _count(db, "experiment.cancelled") == (1 if exp.status == enums.ExperimentStatus.CANCELLED else 0)
        if exp.status == enums.ExperimentStatus.READY:
            assert exp.baseline_observation_id is not None and exp.approved_by == FOUNDER


def test_goal_repointed_before_the_next_transition_blocks_it_under_postgres(engine):
    with Session(engine) as db:
        unit = db.scalar(select(models.BusinessUnit))
        project = models.Project(key="axiomtales", name="A", business_unit_id=unit.id)
        db.add(project)
        db.flush()
        goal = models.Goal(title="g", metric="views", target="10", status=enums.GoalStatus.ACTIVE,
                           project_id=project.id, metric_selector={k: v for k, v in SEL.items() if k != "unit"})
        db.add(goal)
        db.commit()
        key, gid = _propose(db, goal_id=goal.id).key, goal.id
    with Session(engine) as db:
        db.get(models.Goal, gid).metric_selector = {"subject": "youtube:channel", "metric": "likes", "window": "28d"}
        db.commit()
    with Session(engine) as db:
        with pytest.raises(ex.ExperimentError, match="goal selector changed"):
            ex.approve_ready(db, _get(db, key), FOUNDER, now=T0)
        db.rollback()
        assert _get(db, key).status == enums.ExperimentStatus.PROPOSED
