"""Business measurements: provenance kept, re-reports idempotent, missing never shown as 0."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import enums, main, metrics_store, models, schemas
from types import SimpleNamespace

LOCAL = SimpleNamespace(state=SimpleNamespace(caller="local"))  # a host caller, as the middleware sets it

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([
        models.BusinessUnit(key="youtube", name="YouTube", status=enums.BusinessUnitStatus.ACTIVE,
                            priority=enums.Priority.P1),
        models.BusinessUnit(key="seerxo", name="Seerxo", status=enums.BusinessUnitStatus.ACTIVE,
                            priority=enums.Priority.P1),
    ])
    db.commit()
    return db


def _views(value, observed, window="24h", video="abc123"):
    return schemas.MetricsIn(business_unit="youtube", observations=[schemas.MetricIn(
        subject=f"youtube:{video}", metric="views", window=window, value=value,
        source="youtube-data-api", observed_at=observed, evidence={"title": "Titanic"})])


def _rows(db):
    return db.execute(select(func.count()).select_from(models.MetricObservation)).scalar_one()


def test_rereporting_the_same_checkpoint_is_idempotent():
    db = _db()
    assert metrics_store.record(db, _views(120, NOW), now=NOW)["inserted"] == 1
    # Break caught: an executor retry doubles the company's numbers.
    assert metrics_store.record(db, _views(120, NOW), now=NOW)["unchanged"] == 1
    assert _rows(db) == 1


def test_same_source_timestamp_with_a_different_value_is_rejected_not_unchanged():
    db = _db()
    assert metrics_store.record(db, _views(120, NOW), now=NOW)["inserted"] == 1
    result = metrics_store.record(db, _views(121, NOW), now=NOW)
    assert result["inserted"] == 0 and result["unchanged"] == 0
    assert len(result["rejected"]) == 1
    assert "conflicting reading" in result["rejected"][0]
    assert _rows(db) == 1
    assert float(db.scalar(select(models.MetricObservation.value))) == 120
    # The legitimate original retry still succeeds after a conflict.
    assert metrics_store.record(db, _views(120, NOW), now=NOW)["unchanged"] == 1


def test_duplicate_at_the_same_timestamp_must_agree_on_unit_and_business_unit():
    db = _db()
    original = _views(120, NOW)
    original.observations[0].unit = "views"
    assert metrics_store.record(db, original, now=NOW)["inserted"] == 1

    mismatched_unit = _views(120, NOW)
    mismatched_unit.observations[0].unit = "clicks"
    unit_result = metrics_store.record(db, mismatched_unit, now=NOW)
    assert len(unit_result["rejected"]) == 1 and unit_result["unchanged"] == 0

    mismatched_unit.observations[0].unit = "views"
    mismatched_unit.business_unit = "seerxo"
    unit_result = metrics_store.record(db, mismatched_unit, now=NOW)
    assert len(unit_result["rejected"]) == 1 and unit_result["unchanged"] == 0
    assert db.scalar(select(models.MetricObservation.business_unit_id)) == (
        db.scalar(select(models.BusinessUnit.id).where(models.BusinessUnit.key == "youtube"))
    )


def test_conflicting_duplicate_does_not_hide_other_good_readings_in_the_same_batch():
    db = _db()
    metrics_store.record(db, _views(120, NOW), now=NOW)
    body = schemas.MetricsIn(business_unit="youtube", observations=[
        _views(999, NOW).observations[0],
        _views(400, NOW, window="72h").observations[0],
    ])
    result = metrics_store.record(db, body, now=NOW)
    assert result["inserted"] == 1 and result["unchanged"] == 0 and len(result["rejected"]) == 1
    assert _rows(db) == 2
    readings = {r.window: float(r.value) for r in db.scalars(select(models.MetricObservation))}
    assert readings == {"24h": 120.0, "72h": 400.0}


def test_same_timestamp_retry_normalizes_to_stored_numeric_precision():
    db = _db()
    assert metrics_store.record(db, _views(1.23459, NOW), now=NOW)["inserted"] == 1
    result = metrics_store.record(db, _views(1.2346, NOW), now=NOW)
    assert result["unchanged"] == 1 and result["rejected"] == []


def test_every_reading_is_kept_as_history():
    db = _db()
    metrics_store.record(db, _views(120, NOW - timedelta(hours=1)), now=NOW)
    # Break caught: a newer channel snapshot overwrote the older one, so no trend existed.
    assert metrics_store.record(db, _views(150, NOW), now=NOW)["inserted"] == 1
    assert metrics_store.record(db, _views(90, NOW - timedelta(hours=5)), now=NOW)["inserted"] == 1
    values = db.execute(select(models.MetricObservation.value)
                        .order_by(models.MetricObservation.observed_at)).scalars().all()
    assert [float(v) for v in values] == [90, 120, 150]


def test_portfolio_shows_each_series_once_at_its_newest_reading():
    db = _db()
    for hours, value in ((6, 100), (0, 130), (12, 70)):
        metrics_store.record(db, _views(value, NOW - timedelta(hours=hours)), now=NOW)
    metrics_store.record(db, _views(400, NOW - timedelta(hours=3), "72h"), now=NOW)
    youtube = next(u for u in metrics_store.portfolio(db, now=NOW) if u["key"] == "youtube")
    assert sorted((m["window"], m["value"]) for m in youtube["metrics"]) == [("24h", 130.0), ("72h", 400.0)]


def test_windows_are_separate_observations():
    db = _db()
    metrics_store.record(db, _views(120, NOW, "24h"), now=NOW)
    metrics_store.record(db, _views(400, NOW, "72h"), now=NOW)
    assert _rows(db) == 2


def test_future_readings_and_bad_values_are_rejected():
    db = _db()
    out = metrics_store.record(db, _views(1, NOW + timedelta(hours=1)), now=NOW)
    assert out["inserted"] == 0 and out["rejected"]
    with pytest.raises(ValidationError):
        _views(float("nan"), NOW)
    with pytest.raises(ValidationError):
        schemas.MetricIn(subject="x", metric="Views!", value=1, source="s", observed_at=NOW)


def test_future_skew_boundary_is_inclusive_to_the_second():
    db = _db()
    edge = NOW + timedelta(minutes=5)
    accepted = metrics_store.record(db, _views(1, edge), now=NOW)
    assert accepted["inserted"] == 1 and accepted["rejected"] == []
    rejected = metrics_store.record(db, _views(2, edge + timedelta(seconds=1)), now=NOW)
    assert rejected["inserted"] == 0
    assert len(rejected["rejected"]) == 1
    assert "observed_at is in the future" in rejected["rejected"][0]
    assert _rows(db) == 1


def test_future_skew_boundary_is_exactly_five_minutes_at_microsecond_resolution():
    db = _db()
    edge = NOW + timedelta(minutes=5)
    just_inside = metrics_store.record(db, _views(1, edge - timedelta(microseconds=1)), now=NOW)
    assert just_inside["inserted"] == 1 and just_inside["rejected"] == []
    just_outside = metrics_store.record(db, _views(2, edge + timedelta(microseconds=1)), now=NOW)
    assert just_outside["inserted"] == 0
    assert len(just_outside["rejected"]) == 1
    assert "observed_at is in the future" in just_outside["rejected"][0]
    assert _rows(db) == 1


def test_unknown_business_unit_is_404():
    db = _db()
    body = _views(1, NOW)
    body.business_unit = "nope"
    with pytest.raises(HTTPException) as e:
        main.record_metrics(body, LOCAL, db)
    assert e.value.status_code == 404


def test_portfolio_distinguishes_verified_stale_and_unavailable():
    db = _db()
    metrics_store.record(db, _views(120, NOW - timedelta(days=1)), now=NOW)
    by_key = {u["key"]: u for u in metrics_store.portfolio(db, now=NOW)}
    # Break caught: a unit with no source rendered as zeros instead of "no verified source".
    assert by_key["seerxo"]["freshness"] == "UNAVAILABLE" and by_key["seerxo"]["metrics"] == []
    assert by_key["youtube"]["freshness"] == "VERIFIED"
    assert by_key["youtube"]["metrics"][0] | {"observed_at": None} == {
        "subject": "youtube:abc123", "metric": "views", "window": "24h", "value": 120.0,
        "unit": None, "source": "youtube-data-api", "observed_at": None, "label": "Titanic",
        "freshness": "VERIFIED"}
    later = {u["key"]: u for u in metrics_store.portfolio(db, now=NOW + timedelta(days=10))}
    assert later["youtube"]["freshness"] == "STALE"


def test_portfolio_keeps_series_freshness_truth_when_sources_have_mixed_ages():
    db = _db()
    def record(unit, source, metric, at):
        return metrics_store.record(db, schemas.MetricsIn(business_unit=unit, observations=[schemas.MetricIn(
            subject=unit, metric=metric, value=1, source=source, observed_at=at)]), now=NOW)
    record("youtube", "youtube-data-api", "views", NOW)  # Fresh YouTube cannot mask another unit.
    record("seerxo", "seerxo-backend", "signups", NOW)
    record("seerxo", "polar-api", "mrr", NOW - timedelta(hours=4))
    units = {u["key"]: u for u in metrics_store.portfolio(db, now=NOW)}
    assert units["youtube"]["freshness"] == "VERIFIED"
    assert units["seerxo"]["freshness"] == "STALE"
    by_source = {m["source"]: m for m in units["seerxo"]["metrics"]}
    assert by_source["seerxo-backend"]["freshness"] == "VERIFIED"
    assert by_source["polar-api"]["freshness"] == "STALE"


def test_unknown_source_series_uses_default_freshness_without_hiding_it():
    db = _db()
    body = schemas.MetricsIn(business_unit="seerxo", observations=[schemas.MetricIn(
        subject="seerxo", metric="other", value=1, source="someone-new",
        observed_at=NOW - metrics_store.STALE_AFTER - timedelta(microseconds=1))])
    metrics_store.record(db, body, now=NOW)
    series = metrics_store.portfolio(db, now=NOW)[0]["metrics"]
    assert series == [series[0]] and series[0]["freshness"] == "STALE"


def test_portfolio_freshness_boundary_is_per_series_and_inclusive():
    db = _db()
    metrics_store.record(db, _views(1, NOW - timedelta(hours=72), "72h"), now=NOW)
    metrics_store.record(db, _views(2, NOW - timedelta(hours=72, microseconds=1), "7d"), now=NOW)
    youtube = next(u for u in metrics_store.portfolio(db, now=NOW) if u["key"] == "youtube")
    freshness = {m["window"]: m["freshness"] for m in youtube["metrics"]}
    assert freshness == {"72h": "VERIFIED", "7d": "STALE"}
    assert youtube["freshness"] == "STALE"


def test_portfolio_per_unit_summary_only_covers_displayed_series():
    db = _db()
    metrics_store.record(db, _views(1, NOW), now=NOW)
    metrics_store.record(db, _views(2, NOW - timedelta(days=4), "72h"), now=NOW)
    all_series = next(u for u in metrics_store.portfolio(db, now=NOW) if u["key"] == "youtube")
    displayed = next(u for u in metrics_store.portfolio(db, now=NOW, per_unit=1) if u["key"] == "youtube")
    assert all_series["freshness"] == "STALE"
    assert displayed["freshness"] == "VERIFIED" and len(displayed["metrics"]) == 1


def test_portfolio_order_and_every_returned_series_freshness_are_stable():
    db = _db()
    metrics_store.record(db, _views(1, NOW), now=NOW)
    metrics_store.record(db, schemas.MetricsIn(business_unit="seerxo", observations=[schemas.MetricIn(
        subject="seerxo", metric="users", value=1, source="seerxo-backend", observed_at=NOW)]), now=NOW)
    portfolio = metrics_store.portfolio(db, now=NOW)
    assert [u["key"] for u in portfolio] == ["seerxo", "youtube"]
    assert all(m["freshness"] in ("VERIFIED", "STALE") for u in portfolio for m in u["metrics"])


def test_freshness_follows_each_sources_cadence():
    # Break caught: a Polar sync dead for six days still shows revenue as VERIFIED.
    assert metrics_store.stale_after("polar-api") == timedelta(hours=3)
    assert metrics_store.stale_after("youtube-data-api") == timedelta(hours=72)
    assert metrics_store.stale_after("someone-new") == metrics_store.STALE_AFTER


def test_one_registry_answers_writers_units_and_freshness():
    from app import api_auth, metric_sources
    # Break caught: a new source added in one place and forgotten in the other.
    for source, entry in metric_sources.SOURCES.items():
        assert metrics_store.stale_after(source) == entry["freshness"]
        for writer in entry["writers"]:
            for unit in entry["units"]:
                assert api_auth.metric_denial(writer, unit, {source}) is None
    assert metric_sources.internal_only("polar-api") and not metric_sources.internal_only("youtube-auto")


def test_series_is_one_series_oldest_first_within_the_window():
    db = _db()
    metrics_store.record(db, _views(10, NOW - timedelta(days=2)), now=NOW)
    metrics_store.record(db, _views(30, NOW - timedelta(days=1)), now=NOW)
    metrics_store.record(db, _views(99, NOW - timedelta(days=1), window="7d"), now=NOW)  # another window
    metrics_store.record(db, _views(55, NOW - timedelta(days=40)), now=NOW)  # outside the 30 days
    out = metrics_store.series(db, source="youtube-data-api", subject="youtube:abc123", metric="views",
                               window="24h", days=30, now=NOW)
    assert [p["value"] for p in out["points"]] == [10.0, 30.0]
    assert [p["observed_at"] for p in out["points"]] == sorted(p["observed_at"] for p in out["points"])


def test_series_window_boundary_is_inclusive_to_the_microsecond():
    db = _db()
    edge = NOW - timedelta(days=30)
    metrics_store.record(db, _views(1, edge), now=NOW)
    metrics_store.record(db, _views(2, edge - timedelta(microseconds=1)), now=NOW)
    out = metrics_store.series(db, source="youtube-data-api", subject="youtube:abc123", metric="views",
                               window="24h", days=30, now=NOW)
    assert [p["value"] for p in out["points"]] == [1.0]


def test_a_series_with_no_readings_is_empty_never_zero():
    db = _db()
    out = metrics_store.series(db, source="polar-api", subject="polar", metric="mrr", now=NOW)
    assert out["points"] == [] and out["unit"] is None


def test_series_endpoint_passes_its_parameters_to_the_reader():
    db = _db()
    now = datetime.now(timezone.utc)
    metrics_store.record(db, _views(7, now - timedelta(hours=3)), now=now)
    out = main.desk_metric_series(source="youtube-data-api", subject="youtube:abc123", metric="views",
                                  window="24h", days=1, db=db)
    assert [p["value"] for p in out["points"]] == [7.0] and out["days"] == 1
    assert main.desk_metric_series(source="youtube-data-api", subject="youtube:abc123", metric="views",
                                   window="7d", days=1, db=db)["points"] == []
