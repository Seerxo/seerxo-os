"""The goal evidence contract: one resolver, stable reason codes, JSON-safe, read-only."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import enums, goal_evidence, goals, metrics_store, models, schemas

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "7d"}


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(models.BusinessUnit(key="youtube", name="AxiomTales", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1))
    db.commit()
    return db


def _observe(db, value, at=NOW, source="youtube-data-api", unit=None):
    metrics_store.record(db, schemas.MetricsIn(business_unit="youtube", observations=[schemas.MetricIn(
        **SEL, value=value, source=source, unit=unit, observed_at=at)]), now=at + timedelta(minutes=5))


def _goal(db, selector=SEL, baseline="10000", target="20000"):
    g = models.Goal(title="Grow channel reach", metric="Views", baseline=baseline, target=target,
                    status=enums.GoalStatus.ACTIVE, metric_selector=selector, created_at=NOW - timedelta(days=5))
    db.add(g)
    db.commit()
    return g


def _ev(db, g):
    return goal_evidence.for_goal(db, g, NOW)


def test_valid_single_source_measurement():
    db = _db()
    g = _goal(db)
    _observe(db, 12_500, at=NOW - timedelta(hours=1))
    e = _ev(db, g)
    row = db.scalar(select(models.MetricObservation))
    assert (e.measurement_status, e.reason_code, e.source, e.observation_id) == (
        "VERIFIED", "MEASUREMENT_VERIFIED", "youtube-data-api", row.id)
    assert (e.latest_value, e.baseline, e.target, e.progress, e.progress_reason_code) == (
        12_500, 10_000, 20_000, 0.25, "PROGRESS_COMPUTED")
    assert e.observed_at == (NOW - timedelta(hours=1)).isoformat() and e.evidence_timestamp == NOW.isoformat()


def test_explicitly_pinned_source_stays_authoritative():
    db = _db()
    g = _goal(db, SEL | {"source": "youtube-data-api"})
    _observe(db, 12_500)
    _observe(db, 20_000, source="youtube-auto")
    e = _ev(db, g)
    assert e.reason_code == "MEASUREMENT_VERIFIED" and e.source == "youtube-data-api" and e.latest_value == 12_500


def test_ambiguous_sources_are_not_a_value():
    db = _db()
    g = _goal(db)
    _observe(db, 12_500)
    _observe(db, 20_000, source="youtube-auto")
    e = _ev(db, g)
    assert (e.measurement_status, e.reason_code) == ("UNAVAILABLE", "SOURCE_AMBIGUOUS")
    assert e.latest_value is None and e.source is None and e.progress is None and e.progress_reason_code is None


def test_conflicting_units():
    db = _db()
    g = _goal(db)
    _observe(db, 12_500, unit="views")
    _observe(db, 12_500, at=NOW - timedelta(minutes=1))
    e = _ev(db, g)
    assert (e.measurement_status, e.reason_code, e.latest_value) == ("UNAVAILABLE", "UNIT_CONFLICT", None)


def test_future_only_observations():
    db = _db()
    g = _goal(db)
    _observe(db, 20_000, at=NOW + timedelta(minutes=1))
    e = _ev(db, g)
    assert (e.measurement_status, e.reason_code, e.latest_value) == ("UNAVAILABLE", "FUTURE_OBSERVATION", None)


def test_missing_measurement_is_not_zero():
    db = _db()
    e = _ev(db, _goal(db))
    assert (e.measurement_status, e.reason_code) == ("UNAVAILABLE", "MEASUREMENT_MISSING")
    assert e.latest_value is None and e.progress is None and e.observation_id is None


def test_stale_measurement_keeps_provenance_but_is_not_verified():
    db = _db()
    g = _goal(db)
    _observe(db, 11_000, at=NOW - timedelta(days=9))
    e = _ev(db, g)
    assert (e.measurement_status, e.reason_code) == ("STALE", "MEASUREMENT_STALE")
    assert e.source == "youtube-data-api" and e.observation_id is not None and e.latest_value == 11_000


@pytest.mark.parametrize("selector", [{"subject": "youtube:channel"}, "views", None, {**SEL, "source": ""},
                                      {"subject": 1, "metric": "views"}])
def test_malformed_persisted_selector(selector):
    db = _db()
    e = _ev(db, _goal(db, selector))
    assert (e.measurement_status, e.reason_code) == ("UNAVAILABLE", "SELECTOR_INVALID")
    json.dumps(e.to_dict())


@pytest.mark.parametrize("baseline,target,code", [("NaN", "20000", "BASELINE_INVALID"),
                                                  (None, "20000", "BASELINE_INVALID"),
                                                  ("10000", "top 10", "TARGET_INVALID"),
                                                  ("10000", "Infinity", "TARGET_INVALID"),
                                                  ("5", "5", "TARGET_EQUALS_BASELINE")])
def test_invalid_baseline_and_target_keep_the_measurement_but_not_progress(baseline, target, code):
    db = _db()
    g = _goal(db, baseline=baseline, target=target)
    _observe(db, 12_500)
    e = _ev(db, g)
    assert e.reason_code == "MEASUREMENT_VERIFIED" and e.latest_value == 12_500
    assert e.progress is None and e.progress_reason_code == code


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None])
def test_non_finite_stored_value_is_invalid_numeric_evidence(monkeypatch, bad):
    from types import SimpleNamespace
    db = _db()
    row = SimpleNamespace(id=7, value=bad, unit=None, source="s", observed_at=NOW)  # the column cannot hold NaN
    monkeypatch.setattr(goals, "resolve", lambda *a: (row, "MEASUREMENT_VERIFIED"))
    e = _ev(db, _goal(db))
    assert (e.measurement_status, e.reason_code, e.latest_value, e.source) == (
        "UNAVAILABLE", "INVALID_NUMERIC_EVIDENCE", None, None)


def test_evidence_and_progress_cannot_disagree_and_output_is_deterministic():
    db = _db()
    for setup in (lambda: None, lambda: _observe(db, 12_500), lambda: _observe(db, 9, source="other")):
        g = _goal(db)
        setup()
        p, e = goals.progress(db, g, NOW), _ev(db, g)
        assert (p["freshness"], p["progress"], (p["latest"] or {}).get("value")) == (
            e.measurement_status, e.progress, e.latest_value)
        assert e.to_dict() == _ev(db, g).to_dict()
        assert json.loads(json.dumps(e.to_dict())) == e.to_dict()


def test_building_evidence_writes_nothing():
    db = _db()
    g = _goal(db)
    _observe(db, 12_500)
    before = [db.scalar(select(func.count()).select_from(m)) for m in (models.MetricObservation, models.Task, models.AuditLog)]
    _ev(db, g)
    assert not db.new and not db.dirty
    assert before == [db.scalar(select(func.count()).select_from(m)) for m in (models.MetricObservation, models.Task, models.AuditLog)]


def test_unknown_codes_are_rejected():
    with pytest.raises(ValueError):
        goal_evidence.GoalEvidence(1, None, "VERIFIED", "MADE_UP", None, None, None, None, None, None, None, None, None, "t")
