"""Seerxo revenue from Polar: read-only GET, cents to dollars, missing stays missing."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import enums, main, metrics_store, models, polar_metrics, schemas
from types import SimpleNamespace

LOCAL = SimpleNamespace(state=SimpleNamespace(caller="local"))  # a host caller, as the middleware sets it

NOW = datetime(2026, 10, 6, 9, 17, tzinfo=timezone.utc)
POLAR = {
    "periods": [
        {"timestamp": "2026-10-05T00:00:00Z", "revenue": 900, "monthly_recurring_revenue": 4500,
         "active_subscriptions": 4},
        {"timestamp": "2026-10-06T00:00:00Z", "revenue": 1500, "monthly_recurring_revenue": 6000,
         "active_subscriptions": 5},
    ],
    "totals": {"revenue": 4200, "new_subscriptions": 2, "canceled_subscriptions": 1},
}


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(models.BusinessUnit(key="seerxo", name="Seerxo", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1))
    db.commit()
    return db


LIMITS = {"min_date": "2023-12-25", "intervals": {"month": {"min_days": 1, "max_days": 3650},
                                                  "year": {"min_days": 1, "max_days": 36500}}}
LIFETIME = {"periods": [], "totals": {"revenue": 123456}}


def _client(payload, seen, limits=LIMITS, lifetime=LIFETIME):
    """Polar stand-in: limits, the month (interval=day) and all-time (any other interval)."""
    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/v1/metrics/limits":
            return httpx.Response(200, json=limits) if limits else httpx.Response(403, json={})
        return httpx.Response(200, json=payload if request.url.params["interval"] == "day" else lifetime)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sync_reads_polar_once_and_records_seerxo_revenue_with_provenance():
    db, seen = _db(), []
    assert polar_metrics.sync(db, "tok", NOW, _client(POLAR, seen))["inserted"] == 7

    req, limits, lifetime = seen
    assert {r.method for r in seen} == {"GET"} and limits.url.path == "/v1/metrics/limits"
    assert dict(lifetime.url.params) == {"start_date": "2023-12-25", "end_date": "2026-10-06",
                                         "interval": "month", "timezone": "UTC"}
    assert req.method == "GET" and req.url.path == "/v1/metrics/"
    assert dict(req.url.params) == {"start_date": "2026-10-01", "end_date": "2026-10-06",
                                    "interval": "day", "timezone": "UTC"}
    assert req.headers["authorization"] == "Bearer tok"
    rows = {(r.metric, r.window): r for r in db.execute(select(models.MetricObservation)).scalars()}
    assert {k: float(r.value) for k, r in rows.items()} == {
        ("revenue", "today"): 15.0, ("revenue", "mtd"): 42.0, ("mrr", ""): 60.0,
        ("active_subscriptions", ""): 5, ("new_subscriptions", "mtd"): 2, ("cancellations", "mtd"): 1,
        ("revenue", "all_time"): 1234.56}
    assert rows[("revenue", "all_time")].evidence["period_start"] == "2023-12-25"
    assert {r.source for r in rows.values()} == {"polar-api"}
    assert rows[("mrr", "")].unit == "USD" and rows[("active_subscriptions", "")].unit is None
    assert rows[("revenue", "mtd")].evidence["polar_field"] == "revenue"

    # The same hour re-run is idempotent.
    assert polar_metrics.sync(db, "tok", NOW, _client(POLAR, []))["unchanged"] == 7


def test_fields_polar_did_not_return_are_omitted_not_zero():
    db = _db()
    partial = {"periods": [{"timestamp": "2026-10-06T00:00:00Z", "revenue": None}], "totals": {}}
    assert polar_metrics.sync(db, "tok", NOW, _client(partial, [], lifetime={"totals": {}}))["inserted"] == 0
    assert db.execute(select(func.count()).select_from(models.MetricObservation)).scalar_one() == 0
    assert metrics_store.revenue(db, NOW) is None


def test_revenue_summary_never_reports_yesterday_as_today():
    db = _db()
    polar_metrics.sync(db, "tok", NOW, _client(POLAR, []))
    same_day = metrics_store.revenue(db, NOW + timedelta(hours=1))
    assert (same_day["today"], same_day["month"], same_day["mrr"], same_day["total"]) == (15.0, 42.0, 60.0, 1234.56)
    assert same_day["source"] == "polar-api" and same_day["freshness"] == "VERIFIED"

    next_month = metrics_store.revenue(db, datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc))
    assert next_month["today"] is None and next_month["month"] is None
    assert (next_month["mrr"], next_month["total"]) == (60.0, 1234.56)  # snapshots, not periods


def test_revenue_missing_series_remain_none_with_none_metadata():
    db = _db()
    metrics_store.record(db, schemas.MetricsIn(business_unit="seerxo", observations=[schemas.MetricIn(
        subject="seerxo", metric="mrr", value=60, unit="USD", source="polar-api", observed_at=NOW)]), now=NOW)
    out = metrics_store.revenue(db, NOW)
    assert (out["today"], out["month"], out["mrr"], out["total"]) == (None, None, 60.0, None)
    assert out["series"] == {"today": None, "month": None,
                             "mrr": {"source": "polar-api", "observed_at": NOW.isoformat(),
                                     "freshness": "VERIFIED"}, "total": None}


def test_revenue_uses_utc_day_and_month_at_rollover():
    db = _db()
    now = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
    observations = [
        schemas.MetricIn(subject="seerxo", metric="revenue", window="today", value=15, unit="USD",
                         source="polar-api", observed_at=datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)),
        schemas.MetricIn(subject="seerxo", metric="revenue", window="mtd", value=42, unit="USD",
                         source="polar-api", observed_at=datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)),
    ]
    metrics_store.record(db, schemas.MetricsIn(business_unit="seerxo", observations=observations), now=now)
    out = metrics_store.revenue(db, now)
    assert (out["today"], out["month"]) == (15.0, 42.0)


def test_revenue_freshness_boundary_is_inclusive():
    db = _db()
    observations = [
        schemas.MetricIn(subject="seerxo", metric="mrr", value=60, unit="USD", source="polar-api",
                         observed_at=NOW - timedelta(hours=3)),
        schemas.MetricIn(subject="seerxo", metric="revenue", window="all_time", value=1234, unit="USD",
                         source="polar-api", observed_at=NOW - timedelta(hours=3, microseconds=1)),
    ]
    metrics_store.record(db, schemas.MetricsIn(business_unit="seerxo", observations=observations), now=NOW)
    out = metrics_store.revenue(db, NOW)
    assert out["series"]["mrr"]["freshness"] == "VERIFIED"
    assert out["series"]["total"]["freshness"] == out["freshness"] == "STALE"


def test_revenue_mixed_age_series_are_not_represented_as_wholly_fresh():
    db = _db()
    observations = [
        schemas.MetricIn(subject="seerxo", metric="revenue", window="today", value=15, unit="USD",
                         source="polar-api", observed_at=NOW),
        schemas.MetricIn(subject="seerxo", metric="revenue", window="mtd", value=42, unit="USD",
                         source="polar-api", observed_at=NOW - timedelta(hours=4)),
        schemas.MetricIn(subject="seerxo", metric="mrr", value=60, unit="USD",
                         source="polar-api", observed_at=NOW),
        schemas.MetricIn(subject="seerxo", metric="revenue", window="all_time", value=1234, unit="USD",
                         source="polar-api", observed_at=NOW - timedelta(hours=4)),
    ]
    metrics_store.record(db, schemas.MetricsIn(business_unit="seerxo", observations=observations), now=NOW)
    out = metrics_store.revenue(db, NOW)
    assert out["freshness"] == "STALE"
    assert out["series"]["today"]["freshness"] == out["series"]["mrr"]["freshness"] == "VERIFIED"
    assert out["series"]["month"]["freshness"] == out["series"]["total"]["freshness"] == "STALE"


def test_missing_token_records_nothing(monkeypatch, capsys):
    monkeypatch.delenv("POLAR_METRICS_TOKEN", raising=False)
    assert polar_metrics.main() == 2
    assert "not set" in capsys.readouterr().err


def test_no_http_caller_can_report_polar_revenue():
    db = _db()
    forged = schemas.MetricsIn(business_unit="seerxo", observations=[schemas.MetricIn(
        subject="seerxo", metric="mrr", value=99999, source="polar-api", observed_at=NOW)])
    with pytest.raises(HTTPException) as e:
        main.record_metrics(forged, LOCAL, db)
    assert e.value.status_code == 403 and metrics_store.revenue(db, NOW) is None


def test_all_time_failure_keeps_the_month_and_records_no_total():
    db = _db()
    assert polar_metrics.sync(db, "tok", NOW, _client(POLAR, [], limits=None))["inserted"] == 6
    assert metrics_store.revenue(db, NOW)["total"] is None


def test_all_time_is_omitted_when_no_interval_covers_the_range():
    db = _db()
    short = {"min_date": "2023-12-25", "intervals": {"month": {"max_days": 30}, "year": {"max_days": 30}}}
    seen = []
    assert polar_metrics.sync(db, "tok", NOW, _client(POLAR, seen, limits=short))["inserted"] == 6
    assert len(seen) == 2  # no truncated "all-time" query was made


# --- malformed or contradictory Polar readings are never trusted (same contract as the funnel) ------------

def _raw_client(body: str, status=200, limits=LIMITS, lifetime=LIFETIME):
    """Like _client, but the month response is sent as raw JSON text (Python's json also emits NaN/Infinity)."""
    def handler(request: httpx.Request):
        if request.url.path == "/v1/metrics/limits":
            return httpx.Response(200, json=limits)
        if request.url.params["interval"] == "day":
            return httpx.Response(status, content=body, headers={"content-type": "application/json"})
        return httpx.Response(200, json=lifetime)
    return httpx.Client(transport=httpx.MockTransport(handler))


def _month(**period):
    import json
    return json.dumps({"periods": [{"timestamp": "2026-10-06T00:00:00Z", **period}], "totals": {}})


def _rows(db):
    return {(r.metric, r.window): float(r.value) for r in db.execute(select(models.MetricObservation)).scalars()}


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_reading_is_rejected_by_name_and_does_not_lose_the_valid_ones(bad):
    db = _db()
    out = polar_metrics.sync(db, "tok", NOW, _raw_client(_month(revenue=bad if False else 100, monthly_recurring_revenue=bad,
                                                                active_subscriptions=5).replace('"' + bad + '"', bad)))
    # Break caught: pydantic raised for the whole batch, so one bad field cost every good one (and crashed the run).
    assert out["inserted"] >= 2 and any("mrr" in r for r in out["rejected"])
    rows = _rows(db)
    assert ("mrr", "") not in rows and rows[("revenue", "today")] == 1.0 and rows[("active_subscriptions", "")] == 5


@pytest.mark.parametrize("field,value", [("active_subscriptions", -1), ("active_subscriptions", 2.5),
                                         ("monthly_recurring_revenue", -100)])
def test_negative_fractional_or_impossible_values_are_never_persisted(field, value):
    db = _db()
    period = {"revenue": 100, "monthly_recurring_revenue": 6000, "active_subscriptions": 5, field: value}
    out = polar_metrics.sync(db, "tok", NOW, _client({"periods": [{"timestamp": "2026-10-06T00:00:00Z", **period}],
                                                      "totals": {}}, [], lifetime={"totals": {}}))
    name = {"active_subscriptions": "active_subscriptions", "monthly_recurring_revenue": "mrr"}[field]
    assert [r for r in out["rejected"] if name in r] and (name, "") not in _rows(db)
    assert ("revenue", "today") in _rows(db)                                  # independent measurements survive


def test_legitimate_zero_and_a_negative_net_revenue_are_kept():
    db = _db()
    polar_metrics.sync(db, "tok", NOW, _client({"periods": [{"timestamp": "2026-10-06T00:00:00Z", "revenue": -500,
                                                              "monthly_recurring_revenue": 0, "active_subscriptions": 0}],
                                                 "totals": {"new_subscriptions": 0, "canceled_subscriptions": 0}}, [],
                                                lifetime={"totals": {}}))
    rows = _rows(db)
    assert rows[("revenue", "today")] == -5.0 and rows[("mrr", "")] == 0 and rows[("active_subscriptions", "")] == 0
    assert rows[("new_subscriptions", "mtd")] == 0 and rows[("cancellations", "mtd")] == 0   # zero is a reading, not "missing"


@pytest.mark.parametrize("body", ["[]", '"x"', "5", "null", '{"periods": "x", "totals": []}', '{"periods": [1, "a"], "totals": 3}',
                                  "<html>Bearer " + "t" * 40, ""])
def test_a_malformed_envelope_never_raises_or_leaks_and_trusts_nothing_from_the_bad_block(body):
    db = _db()
    out = polar_metrics.sync(db, "t" * 40, NOW, _raw_client(body))
    assert out["rejected"] and "t" * 20 not in str(out)                       # said out loud, no body echoed
    # nothing from the malformed month response; only the independent, valid all-time total may exist
    assert set(_rows(db)) <= {("revenue", "all_time")} and out["inserted"] == len(_rows(db))
    assert metrics_store.revenue(db, NOW) is None or metrics_store.revenue(db, NOW)["today"] is None


def test_the_same_poll_twice_is_idempotent():
    db = _db()
    first = polar_metrics.sync(db, "tok", NOW, _client(POLAR, []))
    again = polar_metrics.sync(db, "tok", NOW, _client(POLAR, []))
    assert first["inserted"] == 7 and again == {"inserted": 0, "unchanged": 7, "rejected": []}


def test_conflicting_repeat_poll_reports_store_and_input_rejections():
    db = _db()
    polar_metrics.sync(db, "tok", NOW, _client(POLAR, []))
    changed = {**POLAR,
               "periods": [POLAR["periods"][0], {**POLAR["periods"][1], "active_subscriptions": -1}],
               "totals": {**POLAR["totals"], "revenue": 4300}}

    result = polar_metrics.sync(db, "tok", NOW, _client(changed, []))

    assert result["inserted"] == 0 and result["unchanged"] == 5
    assert any("active_subscriptions" in reason for reason in result["rejected"])
    assert any("revenue" in reason and "conflicting reading" in reason for reason in result["rejected"])
    assert _rows(db)[("revenue", "mtd")] == 42.0


def test_exit_status_is_nonzero_when_readings_were_rejected(monkeypatch, capsys):
    import contextlib
    from app import db as app_db
    monkeypatch.setenv("POLAR_METRICS_TOKEN", "t" * 40)
    monkeypatch.setattr(app_db, "SessionLocal", lambda: contextlib.nullcontext(object()))
    for result, code in (({"inserted": 7, "unchanged": 0, "rejected": []}, 0), ({"inserted": 0, "unchanged": 7, "rejected": []}, 0),
                         ({"inserted": 0, "unchanged": 0, "rejected": [], "note": "Polar returned no fields"}, 0),
                         ({"inserted": 5, "unchanged": 0, "rejected": ["mrr: not finite"]}, 1)):
        monkeypatch.setattr(polar_metrics, "sync", lambda *a, _r=result, **k: _r)
        assert polar_metrics.main() == code
    assert "mrr: not finite" in capsys.readouterr().err


@pytest.mark.parametrize("lifetime", [[], 5, "x", {"totals": 3}, {"totals": []}, {"totals": {"revenue": False}},
                                      {"totals": {"revenue": "12"}}, {"totals": {"revenue": float("nan")}}])
def test_a_malformed_all_time_response_is_reported_and_the_month_still_counts(lifetime):
    """Codex P2 on PR #195: a falsy or malformed lifetime value was skipped silently, so the run looked clean."""
    db = _db()
    out = polar_metrics.sync(db, "tok", NOW, _client(POLAR, [], lifetime=lifetime))
    assert out["inserted"] == 6 and any("all-time" in r for r in out["rejected"]), out    # month kept, problem said out loud
    assert ("revenue", "all_time") not in _rows(db)


def test_a_genuinely_absent_all_time_total_is_omitted_without_a_rejection():
    db = _db()
    out = polar_metrics.sync(db, "tok", NOW, _client(POLAR, [], lifetime={"totals": {}}))
    assert out == {"inserted": 6, "unchanged": 0, "rejected": []}                      # missing is missing, not an error


def test_an_all_time_total_of_zero_is_a_real_reading():
    db = _db()
    polar_metrics.sync(db, "tok", NOW, _client(POLAR, [], lifetime={"totals": {"revenue": 0}}))
    assert _rows(db)[("revenue", "all_time")] == 0
