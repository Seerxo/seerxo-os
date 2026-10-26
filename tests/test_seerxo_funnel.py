"""Seerxo funnel pull: aggregate counts in, nothing invented, internal-only source."""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import enums, metric_sources, metrics_store, models, seerxo_funnel

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(models.BusinessUnit(key="seerxo", name="Seerxo", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1))
    db.commit()
    return db


def _client(body, status=200, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req.headers.get("authorization"))
        return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})  # allows NaN, as Python's json does upstream
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_counts_become_observations_and_missing_fields_stay_missing():
    db, seen = _db(), []
    out = seerxo_funnel.sync(db, "t" * 40, now=NOW, client=_client({"funnel": {
        "generated_at": "2026-10-06T12:00:00Z", "signups_7d": 12, "activated_7d": 4,
        "activation_rate_7d": 0.3333, "quota_hits_7d": 3, "paid_users": 3,
        "active_users_7d": None}}, seen=seen))
    observations = db.execute(select(models.MetricObservation)).scalars().all()
    rows = {(r.metric, r.window): float(r.value) for r in observations}
    assert out["inserted"] == 5 and seen == ["Bearer " + "t" * 40]
    assert {metrics_store._aware(r.observed_at) for r in observations} == {NOW}
    assert rows[("activation_rate", "7d")] == 0.3333 and rows[("signups", "7d")] == 12
    # Break caught: a field Seerxo did not report shows as 0 on the Desk.
    assert ("active_users", "7d") not in rows
    assert "t" * 40 not in json.dumps([r.evidence for r in db.execute(select(models.MetricObservation)).scalars()])


def test_generated_at_is_the_source_observation_time_and_offsets_normalize_to_utc():
    db = _db()
    out = seerxo_funnel.sync(db, "t" * 40, now=NOW + timedelta(hours=1), client=_client({"funnel": {
        "generated_at": "2026-10-06T14:00:00+02:00", "signups_7d": 12}}))
    row = db.execute(select(models.MetricObservation)).scalar_one()
    assert out["inserted"] == 1 and metrics_store._aware(row.observed_at) == NOW
    assert row.evidence["generated_at"] == "2026-10-06T14:00:00+02:00"


@pytest.mark.parametrize("generated_at", [None, "", "not-a-time", "2026-10-06T12:00:00"])
def test_missing_malformed_or_naive_source_time_records_nothing(generated_at):
    db = _db()
    out = seerxo_funnel.sync(db, "t" * 40, now=NOW, client=_client({"funnel": {
        "generated_at": generated_at, "signups_7d": 12}}))
    assert out == {"inserted": 0, "unchanged": 0, "rejected": ["source timestamp missing or invalid"],
                   "note": "Seerxo source timestamp missing or invalid"}
    assert db.execute(select(models.MetricObservation)).first() is None


def test_old_successful_snapshot_stays_stale_and_the_same_snapshot_is_idempotent():
    db = _db()
    generated = NOW - metric_sources.freshness("seerxo-backend") - timedelta(seconds=1)
    body = {"funnel": {"generated_at": generated.isoformat(), "signups_7d": 12}}
    first = seerxo_funnel.sync(db, "t" * 40, now=NOW, client=_client(body))
    second = seerxo_funnel.sync(db, "t" * 40, now=NOW + timedelta(hours=1), client=_client(body))
    assert first["inserted"] == 1 and second["unchanged"] == 1
    assert metrics_store.portfolio(db, now=NOW)[0]["freshness"] == "STALE"


@pytest.mark.parametrize("delta, inserted, rejected", [
    (timedelta(minutes=5), 1, 0), (timedelta(minutes=5, microseconds=1), 0, 1),
])
def test_source_time_uses_existing_future_skew_boundary(delta, inserted, rejected):
    db = _db()
    out = seerxo_funnel.sync(db, "t" * 40, now=NOW, client=_client({"funnel": {
        "generated_at": (NOW + delta).isoformat(), "signups_7d": 12}}))
    assert out["inserted"] == inserted and len(out["rejected"]) == rejected


def test_http_error_raises_and_records_nothing():
    db = _db()
    try:
        seerxo_funnel.sync(db, "t" * 40, now=NOW, client=_client({}, status=401))
    except httpx.HTTPStatusError:
        pass
    assert db.execute(select(models.MetricObservation)).first() is None


def test_source_is_internal_only_with_an_hourly_freshness():
    assert metric_sources.internal_only("seerxo-backend")
    assert metric_sources.freshness("seerxo-backend").total_seconds() == 3 * 3600


# --- fail-closed aggregate validation ------------------------------------------------------
GOOD = {"generated_at": "2026-10-06T12:00:00Z", "total_users": 100, "signups_1d": 2, "signups_7d": 12,
        "signups_30d": 40, "activation_cohort": 12, "activated_7d": 4, "activation_rate_7d": 0.3333,
        "active_users_7d": 30, "quota_hits_7d": 3, "paid_users": 3, "checkout_started_7d": 2,
        "new_signups_generated_7d": 5, "new_signups_failed_only_7d": 2}


def _rows(db):
    return db.execute(select(models.MetricObservation)).scalars().all()


def _sync(db, funnel, now=NOW):
    return seerxo_funnel.sync(db, "t" * 40, now=now, client=_client({"funnel": funnel}))


def test_a_complete_consistent_snapshot_is_recorded_in_full():
    db = _db()
    assert _sync(db, GOOD)["inserted"] == 13


def test_legitimate_zeros_are_kept_and_an_undefined_rate_is_not_cross_checked():
    db = _db()
    funnel = {**GOOD, "signups_1d": 0, "signups_7d": 0, "signups_30d": 0, "activation_cohort": 0,
              "activated_7d": 0, "activation_rate_7d": 0, "paid_users": 0, "active_users_7d": 0,
              "new_signups_generated_7d": 0, "new_signups_failed_only_7d": 0}
    out = _sync(db, funnel)
    assert out["inserted"] == 13 and {float(r.value) for r in _rows(db) if r.metric == "paid_users"} == {0.0}


@pytest.mark.parametrize("field", ["total_users", "signups_1d", "signups_7d", "signups_30d", "activation_cohort",
                                   "activated_7d", "active_users_7d", "quota_hits_7d", "paid_users", "checkout_started_7d",
                                   "new_signups_generated_7d", "new_signups_failed_only_7d"])
@pytest.mark.parametrize("bad", [-1, 2.5, float("nan"), float("inf"), "12", True, 10**15, [1], {"n": 1}])
def test_an_invalid_count_rejects_the_whole_snapshot(field, bad):
    db = _db()
    out = _sync(db, {"generated_at": GOOD["generated_at"], field: bad})
    # Break caught: a negative or fractional count becomes a trusted observation.
    assert out["inserted"] == 0 and len(out["rejected"]) == 1 and field in out["rejected"][0]
    assert _rows(db) == []


@pytest.mark.parametrize("bad", [-0.1, 1.0001, 7, float("nan"), float("-inf"), "0.5", False])
def test_an_invalid_activation_rate_rejects_the_snapshot(bad):
    db = _db()
    out = _sync(db, {**GOOD, "activation_rate_7d": bad})
    assert out["inserted"] == 0 and "activation_rate_7d" in out["rejected"][0] and _rows(db) == []


def test_integer_valued_floats_and_rate_bounds_are_accepted():
    db = _db()
    assert _sync(db, {"generated_at": GOOD["generated_at"], "signups_7d": 12.0, "activation_rate_7d": 1})["inserted"] == 2


@pytest.mark.parametrize("patch", [
    {"signups_1d": 13},                       # a day cannot out-sign its week
    {"signups_30d": 11},                      # nor a week its month
    {"activated_7d": 13},                     # more activated than the cohort
    {"paid_users": 101}, {"active_users_7d": 101},   # more than all users
    {"activation_rate_7d": 0.9},              # disagrees with 4 / 12
])
def test_contradictory_aggregates_publish_nothing(patch):
    db = _db()
    out = _sync(db, {**GOOD, **patch})
    assert out["inserted"] == 0 and out["rejected"] and "inconsistent" in out["rejected"][0] and _rows(db) == []


def test_cross_checks_only_apply_when_both_fields_are_present():
    db = _db()
    # signups_1d alone, activated without a cohort, paid without total: nothing to compare.
    out = _sync(db, {"generated_at": GOOD["generated_at"], "signups_1d": 99, "activated_7d": 99, "paid_users": 99,
                     "active_users_7d": None})
    assert out["inserted"] == 3


@pytest.mark.parametrize("body", [[], "funnel", 5, {"funnel": []}, {"funnel": "x"}, {"funnel": 3}, {"funnel": None}, {}])
def test_a_malformed_envelope_records_nothing_and_does_not_raise(body):
    db = _db()
    out = seerxo_funnel.sync(db, "t" * 40, now=NOW, client=_client(body))
    assert out["inserted"] == 0 and out["rejected"] == ["upstream response is not a funnel object"]
    assert _rows(db) == []


def test_a_non_json_body_records_nothing_and_leaks_no_token():
    db = _db()
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html>Bearer " + "t" * 40)))
    out = seerxo_funnel.sync(db, "t" * 40, now=NOW, client=client)
    assert out["inserted"] == 0 and "t" * 40 not in json.dumps(out) and _rows(db) == []


def test_rejection_messages_never_echo_upstream_values():
    out = _sync(_db(), {"generated_at": GOOD["generated_at"], "paid_users": "secret-" + "t" * 40})
    assert "t" * 20 not in json.dumps(out)


def test_a_rejected_snapshot_keeps_the_older_valid_reading_and_a_retry_is_idempotent():
    db = _db()
    _sync(db, {**GOOD, "generated_at": "2026-10-06T10:00:00Z"})
    bad = {**GOOD, "paid_users": -3}
    assert _sync(db, bad)["inserted"] == 0
    assert len(_rows(db)) == 13
    assert _sync(db, {**GOOD, "generated_at": "2026-10-06T10:00:00Z"})["unchanged"] == 13


def test_goal_evidence_never_calls_a_rejected_funnel_metric_verified():
    from app import goal_evidence
    db = _db()
    goal = models.Goal(title="Paying customers", metric="paid users", baseline="0", target="10",
                       status=enums.GoalStatus.ACTIVE,
                       metric_selector={"subject": "seerxo:funnel", "metric": "paid_users", "window": ""})
    db.add(goal)
    db.commit()
    _sync(db, {**GOOD, "paid_users": -3})
    ev = goal_evidence.for_goal(db, goal, NOW)
    assert (ev.measurement_status, ev.reason_code, ev.latest_value) == ("UNAVAILABLE", "MEASUREMENT_MISSING", None)
    _sync(db, GOOD)
    assert goal_evidence.for_goal(db, goal, NOW).measurement_status == "VERIFIED"


# --- final hardening: oversized integers and the CLI exit status ---------------------------
@pytest.mark.parametrize("big", [10**400, -10**400, 10**13])
def test_an_oversized_integer_is_rejected_not_a_crash(big):
    db = _db()
    # Break caught: math.isfinite(10**400) raises OverflowError and kills the collector.
    out = seerxo_funnel.sync(db, "t" * 40, now=NOW, client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content='{"funnel": {"generated_at": "2026-10-06T12:00:00Z", '
                                                f'"paid_users": {big}}}}}', headers={"content-type": "application/json"}))))
    assert out["inserted"] == 0 and out["rejected"] == ["paid_users: not a non-negative whole count"]
    assert str(big)[:20] not in json.dumps(out) and _rows(db) == []


def test_an_oversized_integer_rate_is_rejected():
    out = _sync(_db(), {"generated_at": GOOD["generated_at"], "activation_rate_7d": 10**400})
    assert out["inserted"] == 0 and out["rejected"] == ["activation_rate_7d: outside 0..1"]


def test_max_count_itself_is_still_accepted():
    assert _sync(_db(), {"generated_at": GOOD["generated_at"], "total_users": seerxo_funnel.MAX_COUNT})["inserted"] == 1


def _run_main(monkeypatch, capsys, result=None, error=None, token="t" * 40):
    import contextlib
    from app import db as app_db
    if token:
        monkeypatch.setenv("SEERXO_FUNNEL_TOKEN", token)
    else:
        monkeypatch.delenv("SEERXO_FUNNEL_TOKEN", raising=False)
    monkeypatch.setattr(app_db, "SessionLocal", lambda: contextlib.nullcontext(object()))

    def fake_sync(*a, **k):
        if error:
            raise error
        return result
    monkeypatch.setattr(seerxo_funnel, "sync", fake_sync)
    code = seerxo_funnel.main()
    return code, capsys.readouterr()


@pytest.mark.parametrize("result,code", [
    ({"inserted": 10, "unchanged": 0, "rejected": []}, 0),
    ({"inserted": 0, "unchanged": 10, "rejected": []}, 0),
    ({"inserted": 0, "unchanged": 0, "rejected": [], "note": "Seerxo returned no fields"}, 0),
    ({"inserted": 0, "unchanged": 0, "rejected": ["paid_users: not finite"]}, 1),
    ({"inserted": 0, "unchanged": 0, "rejected": ["source timestamp missing or invalid"]}, 1),
    ({"inserted": 3, "unchanged": 0, "rejected": ["x: observed_at is in the future"]}, 1),
])
def test_exit_status_reflects_a_rejected_snapshot(monkeypatch, capsys, result, code):
    status, out = _run_main(monkeypatch, capsys, result)
    assert status == code and "seerxo-funnel:" in out.out + out.err


def test_a_rejected_snapshot_prints_its_reasons_to_stderr_without_the_token(monkeypatch, capsys):
    status, out = _run_main(monkeypatch, capsys, {"inserted": 0, "unchanged": 0, "rejected": ["paid_users: not finite"]})
    assert status == 1 and "paid_users: not finite" in out.err and "t" * 40 not in out.out + out.err


def test_missing_token_still_exits_2_and_http_errors_still_exit_1(monkeypatch, capsys):
    assert _run_main(monkeypatch, capsys, token="")[0] == 2
    resp = httpx.Response(401, request=httpx.Request("GET", seerxo_funnel.DEFAULT_URL))
    status, out = _run_main(monkeypatch, capsys, error=httpx.HTTPStatusError("x", request=resp.request, response=resp))
    assert status == 1 and "HTTP 401" in out.err


def test_an_empty_cohort_leaves_the_old_rate_stale_per_series_and_never_becomes_zero():
    """Live 2026-10-09: no signups 7-14 days ago, so Seerxo omits activation_rate_7d. The Desk then shows the last
    rate as stale while every other series is current: honest per series, and no new zero is invented."""
    db = _db()
    full = {**GOOD, "generated_at": "2026-10-08T10:27:00Z", "activation_cohort": 3, "activated_7d": 0,
            "activation_rate_7d": 0.0}
    assert _sync(db, full, now=datetime(2026, 10, 8, 10, 28, tzinfo=timezone.utc))["inserted"] == 13
    empty = {k: v for k, v in GOOD.items() if k != "activation_rate_7d"}
    empty |= {"generated_at": "2026-10-09T15:27:00Z", "activation_cohort": 0, "activated_7d": 0}
    out = _sync(db, empty, now=datetime(2026, 10, 9, 15, 28, tzinfo=timezone.utc))
    assert out["inserted"] == 12 and out["rejected"] == []                     # twelve readings, the rate is simply absent
    rate = [r for r in _rows(db) if r.metric == "activation_rate"]
    assert len(rate) == 1 and rate[0].observed_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 8, 10, 27, tzinfo=timezone.utc)
    seerxo = next(u for u in metrics_store.portfolio(db, now=datetime(2026, 10, 9, 15, 47, tzinfo=timezone.utc)) if u["key"] == "seerxo")
    fresh = {m["metric"] + "/" + m["window"]: m["freshness"] for m in seerxo["metrics"]}
    assert fresh["activation_rate/7d"] == "STALE" and fresh["signups/7d"] == "VERIFIED" == fresh["paid_users/"]
    assert seerxo["freshness"] == "STALE"                                       # the unit says "some series is stale", per the Desk contract


def test_checkout_started_is_recorded_as_its_own_series_never_as_a_purchase():
    db = _db()
    _sync(db, GOOD)
    rows = {(r.metric, r.window): float(r.value) for r in _rows(db)}
    assert rows[("checkout_started", "7d")] == 2 and rows[("paid_users", "")] == 3   # two different populations
    assert "conversion" not in {m for m, _ in rows}                                  # no rate is derived from them


def test_more_checkout_starters_than_users_is_a_contradiction_and_rejects_the_snapshot():
    db = _db()
    out = _sync(db, {**GOOD, "checkout_started_7d": 101})        # total_users is 100
    assert out["inserted"] == 0 and out["rejected"] and _rows(db) == []


def test_new_signup_splits_are_recorded_and_cannot_exceed_their_whole():
    db = _db()
    _sync(db, GOOD)   # 5 generated + 2 failed-only of 12 signups: 5 not tried yet
    rows = {(r.metric, r.window): float(r.value) for r in _rows(db)}
    assert rows[("new_signups_generated", "7d")] == 5 and rows[("new_signups_failed_only", "7d")] == 2
    for bad in ({"new_signups_generated_7d": 13}, {"new_signups_generated_7d": 8, "new_signups_failed_only_7d": 5}):
        db2 = _db()
        out = _sync(db2, {**GOOD, **bad})                     # each <= 12 alone, or together past signups_7d = 12
        assert out["inserted"] == 0 and out["rejected"] and _rows(db2) == []
