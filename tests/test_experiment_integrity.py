"""Experiment core integrity: goal consistency, evidence limits, identity, duplicates, rollback, learnings."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, enums, experiments as ex, metrics_store, models

FOUNDER = "founder:access:founder@example.com"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "28d", "source": "youtube-analytics-api",
       "unit": "count"}
GOAL_SEL = {"subject": "youtube:channel", "metric": "views", "window": "28d", "source": "youtube-analytics-api"}
ROOT = Path(__file__).resolve().parent.parent


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    yt = models.BusinessUnit(key="youtube", name="AxiomTales", status=enums.BusinessUnitStatus.ACTIVE,
                             priority=enums.Priority.P1)
    other = models.BusinessUnit(key="seerxo", name="Seerxo", status=enums.BusinessUnitStatus.ACTIVE,
                                priority=enums.Priority.P1)
    db.add_all([yt, other])
    db.flush()
    db.add_all([models.Project(key="axiomtales", name="A", business_unit_id=yt.id),
                models.Project(key="seerxo-app", name="S", business_unit_id=other.id)])
    db.commit()
    return db


def _obs(db, value, at, unit="count", source="youtube-analytics-api", metric="views", window="28d"):
    unit_id = db.scalar(select(models.BusinessUnit.id).where(models.BusinessUnit.key == "youtube"))
    row = models.MetricObservation(business_unit_id=unit_id, subject="youtube:channel", metric=metric,
                                   window=window, value=value, unit=unit, source=source, observed_at=at)
    db.add(row)
    db.commit()
    return row


def _goal(db, selector=GOAL_SEL, project="axiomtales", status=enums.GoalStatus.ACTIVE):
    p = db.scalar(select(models.Project).where(models.Project.key == project)) if project else None
    g = models.Goal(title="Grow views", metric="views", baseline="0", target="100000", status=status,
                    metric_selector=selector, project_id=p.id if p else None)
    db.add(g)
    db.commit()
    return g


def _propose(db, **kw):
    args = dict(title="Shorter intros", hypothesis="Shorter intros raise views", business_unit="youtube",
                metric_selector=SEL, expected_direction="UP", measurement_window_hours=48, actor="ceo",
                min_effect=100)
    args.update(kw)
    exp = ex.propose(db, **args)
    db.commit()
    return exp


def _ready(db, **kw):
    if not db.scalar(select(models.MetricObservation.id)):
        _obs(db, 1000, T0 - timedelta(hours=1))
    exp = _propose(db, **kw)
    ex.approve_ready(db, exp, FOUNDER, now=T0)
    db.commit()
    return exp


def _evaluated(db, **kw):
    exp = _ready(db, **kw)
    ex.start(db, exp, "TASK-0100", "developer", now=T0)
    ex.close_window(db, exp, now=T0 + timedelta(hours=49))
    _obs(db, 1500, T0 + timedelta(hours=50))
    ex.evaluate(db, exp, now=T0 + timedelta(hours=51))
    db.commit()
    return exp


def _audits(db, action=None):
    q = select(func.count()).select_from(models.AuditLog).where(models.AuditLog.entity_type == "experiment")
    return db.scalar(q.where(models.AuditLog.action == action) if action else q)


# --- goal consistency -----------------------------------------------------------------------
def test_a_matching_goal_is_linked_and_its_selector_is_snapshotted():
    db = _db()
    g = _goal(db)
    _obs(db, 1000, T0 - timedelta(hours=1))
    exp = _propose(db, goal_id=g.id)
    assert exp.goal_id == g.id and exp.goal_selector_snapshot == GOAL_SEL


@pytest.mark.parametrize("project", ["seerxo-app", None])
def test_a_goal_from_another_or_unknown_business_unit_is_rejected(project):
    db = _db()
    g = _goal(db, project=project)
    with pytest.raises(ex.ExperimentError, match="business unit"):
        _propose(db, goal_id=g.id)
    assert db.scalar(select(func.count()).select_from(models.Experiment)) == 0


def test_an_experiment_project_that_contradicts_the_goal_project_is_rejected():
    db = _db()
    g = _goal(db)
    other = db.scalar(select(models.Project.id).where(models.Project.key == "seerxo-app"))
    with pytest.raises(ex.ExperimentError, match="project"):
        _propose(db, goal_id=g.id, project_id=other)


@pytest.mark.parametrize("field,value", [("subject", "youtube:other"), ("metric", "likes"), ("window", "7d"),
                                         ("source", "youtube-data-api")])
def test_a_goal_measuring_a_different_series_is_rejected(field, value):
    db = _db()
    g = _goal(db, GOAL_SEL | {field: value})
    with pytest.raises(ex.ExperimentError, match="goal"):
        _propose(db, goal_id=g.id)


def test_a_goal_selector_without_a_window_means_the_default_window():
    db = _db()
    sel = {**SEL, "window": ""}
    g = _goal(db, {"subject": "youtube:channel", "metric": "views", "source": "youtube-analytics-api"})
    _obs(db, 1000, T0 - timedelta(hours=1), window="")
    assert _propose(db, goal_id=g.id, metric_selector=sel).goal_id == g.id


def test_a_source_less_goal_selector_must_resolve_to_this_experiments_source():
    db = _db()
    sourceless = {k: v for k, v in GOAL_SEL.items() if k != "source"}
    g = _goal(db, sourceless)
    with pytest.raises(ex.ExperimentError, match="source"):   # no reading proves which reporter it means
        _propose(db, goal_id=g.id)
    _obs(db, 1000, T0 - timedelta(hours=1))
    assert _propose(db, goal_id=g.id).goal_id == g.id          # one reporter, and it is ours
    ex.cancel(db, db.scalar(select(models.Experiment)), "ceo", "retry")
    db.commit()
    _obs(db, 900, T0 - timedelta(hours=1), source="youtube-data-api")  # a second reporter appears
    with pytest.raises(ex.ExperimentError, match="source"):
        _propose(db, goal_id=g.id)


def test_a_source_less_goal_resolving_to_another_source_is_rejected():
    db = _db()
    g = _goal(db, {k: v for k, v in GOAL_SEL.items() if k != "source"})
    _obs(db, 900, T0 - timedelta(hours=1), source="youtube-data-api")
    with pytest.raises(ex.ExperimentError, match="source"):
        _propose(db, goal_id=g.id)


@pytest.mark.parametrize("selector", [{"subject": "youtube:channel"}, {}, None])
def test_a_goal_with_an_invalid_selector_cannot_anchor_an_experiment(selector):
    db = _db()
    g = _goal(db, selector)
    with pytest.raises(ex.ExperimentError, match="goal"):
        _propose(db, goal_id=g.id)


def test_unknown_and_closed_goals_are_rejected():
    db = _db()
    with pytest.raises(ex.ExperimentError, match="unknown goal"):
        _propose(db, goal_id=999)
    for status in (enums.GoalStatus.ACHIEVED, enums.GoalStatus.MISSED, enums.GoalStatus.ABANDONED):
        with pytest.raises(ex.ExperimentError, match="closed"):
            _propose(db, goal_id=_goal(db, status=status).id)


def _changed_goal(db):
    _obs(db, 1000, T0 - timedelta(hours=1))
    g = _goal(db)
    exp = _propose(db, goal_id=g.id)
    g.metric_selector = GOAL_SEL | {"metric": "likes"}   # someone repoints the goal after the proposal
    db.commit()
    return g, exp


def test_a_changed_goal_selector_blocks_ready_and_start_but_not_cancel():
    db = _db()
    g, exp = _changed_goal(db)
    with pytest.raises(ex.ExperimentError, match="goal selector changed"):
        ex.approve_ready(db, exp, FOUNDER, now=T0)
    assert exp.status == enums.ExperimentStatus.PROPOSED
    g.metric_selector = GOAL_SEL
    db.commit()
    ex.approve_ready(db, exp, FOUNDER, now=T0)
    g.metric_selector = GOAL_SEL | {"window": "7d"}
    db.commit()
    with pytest.raises(ex.ExperimentError, match="goal selector changed"):
        ex.start(db, exp, "TASK-1", "dev", now=T0)
    ex.cancel(db, exp, "ceo", "goal repointed")
    assert exp.status == enums.ExperimentStatus.CANCELLED


def test_a_changed_goal_selector_blocks_acceptance_but_allows_rejection():
    db = _db()
    _obs(db, 1000, T0 - timedelta(hours=1))
    g = _goal(db)
    exp = _evaluated(db, goal_id=g.id)
    g.metric_selector = GOAL_SEL | {"metric": "likes"}
    db.commit()
    assert exp.evidence["goal_link"] == {"status": "LINKED", "goal_id": g.id, "mode": "EXACT_SOURCE"}
    with pytest.raises(ex.ExperimentError, match="goal selector changed"):
        ex.decide(db, exp, True, FOUNDER, "ship it", now=T0)
    assert exp.status == enums.ExperimentStatus.EVALUATED
    ex.decide(db, exp, False, FOUNDER, "goal changed; do not reuse", now=T0)
    assert exp.status == enums.ExperimentStatus.REJECTED


def test_a_goal_moved_to_another_business_unit_blocks_the_next_transition():
    db = _db()
    _obs(db, 1000, T0 - timedelta(hours=1))
    g = _goal(db)
    exp = _propose(db, goal_id=g.id)
    g.project_id = db.scalar(select(models.Project.id).where(models.Project.key == "seerxo-app"))
    db.commit()
    with pytest.raises(ex.ExperimentError, match="business unit"):
        ex.approve_ready(db, exp, FOUNDER, now=T0)


def test_evaluation_records_a_broken_goal_link_instead_of_hiding_it():
    db = _db()
    _obs(db, 1000, T0 - timedelta(hours=1))
    g = _goal(db)
    exp = _ready(db, goal_id=g.id)
    ex.start(db, exp, "TASK-1", "dev", now=T0)
    ex.close_window(db, exp, now=T0 + timedelta(hours=49))
    g.metric_selector = None
    db.commit()
    _obs(db, 1500, T0 + timedelta(hours=50))
    ex.evaluate(db, exp, now=T0 + timedelta(hours=51))
    assert exp.evidence["goal_link"]["status"] == "GOAL_SELECTOR_CHANGED"
    with pytest.raises(ex.ExperimentError):
        ex.decide(db, exp, True, FOUNDER, "x", now=T0)


def test_an_experiment_without_a_goal_is_unaffected():
    db = _db()
    exp = _evaluated(db)
    assert exp.evidence["goal_link"] == {"status": "NO_GOAL"}
    ex.decide(db, exp, True, FOUNDER, "worked", now=T0)
    assert exp.status == enums.ExperimentStatus.ACCEPTED


# --- evidence limits ------------------------------------------------------------------------
def test_evidence_never_claims_causality_or_statistical_proof():
    db = _db()
    exp = _evaluated(db)
    ev = exp.evidence
    assert ev["claim"] == "OBSERVED_DIFFERENCE" and ev["causality"].startswith("NOT_ESTABLISHED")
    assert ev["control_group"] is None and ev["statistically_proven"] is False
    ex.decide(db, exp, True, FOUNDER, "worked", now=T0)
    lesson = ex.propose_learning(db, exp, "ceo").lesson.lower()
    assert "causality not established" in lesson and "proven" not in lesson and "caused" not in lesson
    assert "proven" not in str(ex.view(exp)).lower().replace("statistically_proven", "")


def test_a_late_first_reading_after_the_window_is_insufficient_evidence():
    db = _db()
    exp = _ready(db)
    ex.start(db, exp, "TASK-1", "dev", now=T0)
    ex.close_window(db, exp, now=T0 + timedelta(hours=49))
    gap = metrics_store.stale_after("youtube-analytics-api") + timedelta(hours=1)
    _obs(db, 1500, T0 + timedelta(hours=48) + gap)   # nothing was reported for longer than freshness allows
    with pytest.raises(ex.ExperimentError, match="insufficient evidence"):
        ex.evaluate(db, exp, now=T0 + timedelta(hours=48) + gap + timedelta(hours=1))
    assert exp.status == enums.ExperimentStatus.AWAITING_MEASUREMENT


def test_a_baseline_that_went_stale_before_the_change_refuses_to_start():
    db = _db()
    exp = _ready(db)
    late = T0 + metrics_store.stale_after("youtube-analytics-api") + timedelta(hours=1)
    with pytest.raises(ex.ExperimentError, match="stale"):
        ex.start(db, exp, "TASK-1", "dev", now=late)
    assert exp.status == enums.ExperimentStatus.READY


def test_a_baseline_row_from_another_series_is_never_evidence():
    db = _db()
    exp = _ready(db)
    other = _obs(db, 77, T0 - timedelta(minutes=5), metric="likes")
    exp.baseline_observation_id = other.id   # corrupted link
    db.commit()
    with pytest.raises(ex.ExperimentError, match="series"):
        ex.start(db, exp, "TASK-1", "dev", now=T0)


# --- identity -------------------------------------------------------------------------------
@pytest.mark.parametrize("actor,ok", [
    ("founder:access:founder@example.com", True), ("founder:access:a.b+c@sub.example.co", True),
    # the legacy and look-alike forms are not founder authority
    ("founder", False), ("founder (via host)", False), ("founder (github:semih, via ci)", False),
    ("founder:access:", False), ("founder:access:@example.com", False), ("founder:access:Founder@example.com", False),
    ("founder:access:a@example", False), ("founder:access:a@example.com ", False),
    ("founder:access:a@example.com\nx", False), ("xfounder:access:a@example.com", False),
    ("founder:access:" + "a" * 90 + "@example.com", False),
    ("operator:local", False), ("operator:host", False), ("service:admin", False), ("anonymous", False),
    ("Founder", False), ("ceo", False), ("", False), (None, False), (7, False)])
def test_founder_identity_is_exactly_the_verified_principal_id(actor, ok):
    assert ex.is_founder(actor) is ok


@pytest.mark.parametrize("actor", ["", " ", " ceo", "ceo ", "a\nb", "x" * 101, None])
def test_actors_must_be_clean_identities_that_fit_the_column(actor):
    db = _db()
    with pytest.raises(ex.ExperimentError, match="actor"):
        _propose(db, actor=actor)
    exp = _propose(db)
    with pytest.raises(ex.ExperimentError, match="actor"):
        ex.cancel(db, exp, actor, "x")
    assert exp.status == enums.ExperimentStatus.PROPOSED


def test_founder_only_steps_refuse_everyone_else():
    db = _db()
    _obs(db, 1000, T0 - timedelta(hours=1))
    exp = _propose(db)
    for who in ("ceo", "founder", "founder (via host)", "operator:local", "service:admin", "system:experiments"):
        with pytest.raises(ex.NotFounder):
            ex.approve_ready(db, exp, who, now=T0)
    assert exp.status == enums.ExperimentStatus.PROPOSED


# --- duplicates, idempotency, failed transactions -------------------------------------------
def test_a_repeated_proposal_returns_the_open_experiment_without_a_second_row_or_audit():
    db = _db()
    _obs(db, 1000, T0 - timedelta(hours=1))
    first = _propose(db)
    again = _propose(db, title="  Shorter intros ", hypothesis="Shorter intros raise views")
    assert again.id == first.id and db.scalar(select(func.count()).select_from(models.Experiment)) == 1
    assert _audits(db, "experiment.proposed") == 1
    assert _propose(db, hypothesis="Shorter intros raise watch time").id != first.id


def test_a_finished_experiment_does_not_block_a_new_proposal():
    db = _db()
    exp = _evaluated(db)
    ex.decide(db, exp, False, FOUNDER, "inconclusive", now=T0)
    db.commit()
    assert _propose(db).id != exp.id


def test_the_database_itself_refuses_a_second_open_experiment_per_fingerprint():
    db = _db()
    first = _propose(db)
    clone = models.Experiment(
        key="EXP-9999", title="x", hypothesis="x", status=enums.ExperimentStatus.PROPOSED,
        business_unit_id=first.business_unit_id, metric_selector=SEL, expected_direction="UP", min_effect=0,
        measurement_window_hours=1, created_by="x", fingerprint=first.fingerprint)
    db.add(clone)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize("column,value", [("expected_direction", "SIDEWAYS"), ("min_effect", -1),
                                          ("measurement_window_hours", 0)])
def test_the_database_refuses_invalid_rows(column, value):
    db = _db()
    exp = _propose(db)
    setattr(exp, column, value)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_replaying_every_transition_writes_no_second_audit_row():
    db = _db()
    exp = _ready(db)

    def replay(step):
        before = _audits(db)
        step()
        step()
        db.commit()
        assert _audits(db) == before + 1   # the first call wrote one row; the replay wrote none
    replay(lambda: ex.start(db, exp, "TASK-1", "dev", now=T0))
    replay(lambda: ex.close_window(db, exp, now=T0 + timedelta(hours=49)))
    _obs(db, 1500, T0 + timedelta(hours=50))
    replay(lambda: ex.evaluate(db, exp, now=T0 + timedelta(hours=51)))
    replay(lambda: ex.decide(db, exp, True, FOUNDER, "worked", now=T0))
    before = _audits(db)
    ex.propose_learning(db, exp, "ceo")
    ex.propose_learning(db, exp, "ceo")
    db.commit()
    assert _audits(db) == before + 1
    ready = _ready(db, hypothesis="a different hypothesis")   # approve_ready replay, in its own state
    before = _audits(db)
    ex.approve_ready(db, ready, FOUNDER, now=T0)
    assert _audits(db) == before


def test_a_failed_transition_rolls_back_completely_and_can_be_retried(monkeypatch):
    db = _db()
    exp = _ready(db)
    real = audit.record

    def boom(*a, **k):
        raise RuntimeError("audit store down")
    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(RuntimeError):
        ex.start(db, exp, "TASK-1", "dev", now=T0)
    db.rollback()
    db.refresh(exp)
    assert exp.status == enums.ExperimentStatus.READY and exp.intervention_ref is None and exp.measure_after is None
    monkeypatch.setattr(audit, "record", real)
    ex.start(db, exp, "TASK-1", "dev", now=T0)
    db.commit()
    assert exp.status == enums.ExperimentStatus.RUNNING


def test_a_vanished_row_is_a_clear_error_not_an_attribute_error():
    db = _db()
    exp = _propose(db)
    ghost = models.Experiment(id=12345)
    with pytest.raises(ex.ExperimentError, match="not found"):
        ex.cancel(db, ghost, "ceo", "x")
    assert exp.status == enums.ExperimentStatus.PROPOSED


# --- learning pipeline ----------------------------------------------------------------------
@pytest.mark.parametrize("accept", [True, False])
def test_a_learning_from_an_experiment_is_only_ever_a_candidate(accept):
    db = _db()
    exp = _evaluated(db)
    ex.decide(db, exp, accept, FOUNDER, "reviewed", now=T0)
    learning = ex.propose_learning(db, exp, "ceo")
    db.commit()
    assert learning.status == enums.LearningStatus.CANDIDATE
    assert db.scalar(select(func.count()).select_from(models.Learning).where(
        models.Learning.status == enums.LearningStatus.APPROVED)) == 0


def test_the_engine_has_no_code_path_to_an_approved_learning():
    source = (ROOT / "app" / "experiments.py").read_text()
    assert "LearningStatus.APPROVED" not in source and "LearningStatus.CANDIDATE" in source
