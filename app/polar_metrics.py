"""Seerxo revenue from Polar into metric_observations (§102). Read-only by construction.

The token is a Polar organization access token with the single scope `metrics:read`
(POLAR_METRICS_TOKEN). It cannot create checkouts, cancel subscriptions or touch billing;
Seerxo's own Polar token stays in Seerxo. No token, no rows: the Desk keeps saying
"no verified source" instead of showing 0.

Run hourly from cron:  docker exec company-api python -m app.polar_metrics
"""
import os
import sys
from datetime import date, datetime, timezone
from math import isfinite

import httpx
from sqlalchemy.orm import Session

from app import metrics_store, schemas

SOURCE = "polar-api"
UNIT_KEY = "seerxo"
API_BASE = "https://api.polar.sh"
# Polar reports money in cents of the organization's currency; Seerxo's prices are USD.
# ponytail: one currency; read the organization's currency if Seerxo ever prices in another.
CURRENCY = "USD"
MAX_RAW = 10**12   # far above any real figure (cents or count), well inside Numeric(18, 4)


def _invalid(metric: str, raw, kind: str) -> str | None:
    """Why a Polar reading cannot be trusted; None when it can. Never echoes the value.
    kind: count (finite whole number >= 0), mrr (finite >= 0), money (finite; net revenue may be negative after refunds)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return f"{metric}: not a number"
    if isinstance(raw, float) and not isfinite(raw):   # isfinite(int) overflows on huge ints, which fail the range check
        return f"{metric}: not finite"
    if abs(raw) > MAX_RAW:
        return f"{metric}: out of range"
    if kind == "count" and (raw < 0 or raw != int(raw)):
        return f"{metric}: not a non-negative whole count"
    if kind == "mrr" and raw < 0:
        return f"{metric}: negative"
    return None


def _get(c: httpx.Client, token: str, path: str, params: dict | None = None) -> dict:
    r = c.get(f"{API_BASE}{path}", params=params, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def fetch(c: httpx.Client, token: str, today: date) -> dict:
    """Daily periods from the first of the month to today (UTC), plus Polar's totals."""
    return _get(c, token, "/v1/metrics/", {"start_date": today.replace(day=1).isoformat(),
                                           "end_date": today.isoformat(), "interval": "day",
                                           "timezone": "UTC"})


def fetch_lifetime(c: httpx.Client, token: str, today: date) -> tuple[str, dict] | None:
    """Totals from Polar's earliest metrics date to today. Polar's cumulative_revenue only
    runs from the start of the requested range, so all-time needs the whole range. None when
    no interval admits that many days: a shorter range would not be all-time."""
    limits = _get(c, token, "/v1/metrics/limits")
    start = date.fromisoformat(limits["min_date"])
    days = (today - start).days + 1
    for interval in ("month", "year"):
        if days <= int(((limits.get("intervals") or {}).get(interval) or {}).get("max_days") or 0):
            return start.isoformat(), _get(c, token, "/v1/metrics/", {
                "start_date": start.isoformat(), "end_date": today.isoformat(),
                "interval": interval, "timezone": "UTC"})
    return None


def observations(data: dict, today: date, now: datetime, rejected: list[str] | None = None) -> list[schemas.MetricIn]:
    """Map Polar's response to readings. A field Polar did not return is omitted, never 0; a field it returned
    that cannot be trusted is omitted too and named in `rejected` (the other, independent readings still count)."""
    rejected = [] if rejected is None else rejected
    raw_periods, raw_totals = data.get("periods"), data.get("totals")
    if raw_periods is not None and not isinstance(raw_periods, list):
        rejected.append("periods: not a list")
    if raw_totals is not None and not isinstance(raw_totals, dict):
        rejected.append("totals: not an object")
    periods = [p for p in raw_periods if isinstance(p, dict)] if isinstance(raw_periods, list) else []
    if isinstance(raw_periods, list) and len(periods) != len(raw_periods):
        rejected.append("periods: contains non-objects")
    current = next((p for p in periods if str(p.get("timestamp", "")).startswith(today.isoformat())), None)
    totals = raw_totals if isinstance(raw_totals, dict) else {}
    evidence = {"period_start": today.replace(day=1).isoformat(), "period_end": today.isoformat(),
                "timezone": "UTC", "endpoint": "/v1/metrics/"}
    wanted = [  # (Polar block, Polar field, our metric, window, unit, cents?, kind)
        (current, "revenue", "revenue", "today", CURRENCY, True, "money"),
        (totals, "revenue", "revenue", "mtd", CURRENCY, True, "money"),
        (current, "monthly_recurring_revenue", "mrr", "", CURRENCY, True, "mrr"),
        (current, "active_subscriptions", "active_subscriptions", "", None, False, "count"),
        (totals, "new_subscriptions", "new_subscriptions", "mtd", None, False, "count"),
        (totals, "canceled_subscriptions", "cancellations", "mtd", None, False, "count"),
    ]
    out = []
    for block, field, metric, window, unit, cents, kind in wanted:
        raw = (block or {}).get(field)
        if raw is None:
            continue
        if why := _invalid(metric, raw, kind):
            rejected.append(why)
            continue
        out.append(schemas.MetricIn(subject=UNIT_KEY, metric=metric, window=window,
                                    value=raw / 100 if cents else raw, unit=unit, source=SOURCE,
                                    observed_at=now, evidence={**evidence, "polar_field": field}))
    return out


def sync(db: Session, token: str, now: datetime | None = None, client: httpx.Client | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    rejected: list[str] = []
    with client or httpx.Client(timeout=30) as c:
        try:
            data = fetch(c, token, today)
        except ValueError:   # not JSON (httpx.HTTPStatusError is not a ValueError and still propagates); never echo the body
            return {"inserted": 0, "unchanged": 0, "rejected": ["polar response is not JSON"]}
        if not isinstance(data, dict):
            return {"inserted": 0, "unchanged": 0, "rejected": ["polar response is not a metrics object"]}
        obs = observations(data, today, now, rejected)
        try:  # the month's readings are worth keeping even if the all-time query fails
            lifetime = fetch_lifetime(c, token, today)
        except httpx.HTTPError as e:   # transport or status: unavailable, not malformed
            print(f"polar-metrics: all-time revenue unavailable ({type(e).__name__})", file=sys.stderr)
            lifetime = None
        except (KeyError, ValueError, TypeError, AttributeError) as e:   # reachable but not what Polar documents
            print(f"polar-metrics: all-time response malformed ({type(e).__name__})", file=sys.stderr)
            rejected.append("all-time: malformed response")
            lifetime = None
    if lifetime is not None:   # fetch_lifetime returns None when no interval covers the range: nothing to judge
        start, body = lifetime
        totals = body.get("totals") if isinstance(body, dict) else None
        if not isinstance(totals, dict):
            rejected.append("all-time: malformed response")
        elif (total := totals.get("revenue")) is not None:   # absent stays absent; False, "12", NaN are rejected below
            if why := _invalid("revenue all_time", total, "money"):
                rejected.append("all-time: " + why)
            else:
                obs.append(schemas.MetricIn(
                    subject=UNIT_KEY, metric="revenue", window="all_time", value=total / 100, unit=CURRENCY,
                    source=SOURCE, observed_at=now,
                    evidence={"period_start": start, "period_end": today.isoformat(), "timezone": "UTC",
                              "endpoint": "/v1/metrics/", "polar_field": "totals.revenue"}))
    if not obs:
        return {"inserted": 0, "unchanged": 0, "rejected": rejected, "note": "Polar returned no usable fields"}
    stored = metrics_store.record(db, schemas.MetricsIn(business_unit=UNIT_KEY, observations=obs), now)
    return {**stored, "rejected": rejected + stored["rejected"]}


def main() -> int:
    token = os.environ.get("POLAR_METRICS_TOKEN", "")
    if not token:
        print("polar-metrics: POLAR_METRICS_TOKEN not set; nothing recorded", file=sys.stderr)
        return 2
    from app.db import SessionLocal
    try:
        with SessionLocal() as db:
            result = sync(db, token)
            print("polar-metrics:", result)
            if result.get("rejected"):  # reasons name metrics only, never Polar's values
                print("polar-metrics: readings rejected:", "; ".join(result["rejected"]), file=sys.stderr)
                return 1
    except httpx.HTTPStatusError as e:  # status only: the response could echo request details
        print(f"polar-metrics: Polar returned HTTP {e.response.status_code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
