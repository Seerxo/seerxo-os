"""Seerxo's activation funnel into metric_observations (§102). Read-only, aggregate counts only.

Seerxo's frontend sends its funnel events to Vercel custom events, which only Pro/Enterprise
plans record (docs/research/2026-10-06-seerxo-conversion-audit.md). The truth is in Seerxo's
Firestore, so seerxo-backend serves counts at GET /internal/funnel (bearer
SEERXO_FUNNEL_TOKEN, shared with Seerxo's COMPANY_OS_FUNNEL_TOKEN). No ids, emails or names
cross; a field Seerxo did not return is omitted, never 0, and a snapshot with an invalid or
contradictory field is rejected whole (docs/SEERXO_FUNNEL_VALIDATION.md).

Run hourly from cron:  docker exec company-api python -m app.seerxo_funnel
"""
import os
import sys
from math import isfinite
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app import metrics_store, schemas

SOURCE = "seerxo-backend"
UNIT_KEY = "seerxo"
SUBJECT = "seerxo:funnel"
DEFAULT_URL = "https://api.seerxo.com/internal/funnel"
# (Seerxo field, our metric, window, unit)
FIELDS = [
    ("total_users", "users", "", None), ("signups_1d", "signups", "1d", None),
    ("signups_7d", "signups", "7d", None), ("signups_30d", "signups", "30d", None),
    ("activation_cohort", "activation_cohort", "7d", None), ("activated_7d", "activated", "7d", None),
    ("activation_rate_7d", "activation_rate", "7d", "ratio"),
    ("active_users_7d", "active_users", "7d", None), ("quota_hits_7d", "quota_hits", "7d", None),
    ("paid_users", "paid_users", "", None),
    # A started checkout (users, once each), not a purchase: purchases are paid_users and Polar's subscriptions.
    ("checkout_started_7d", "checkout_started", "7d", None),
    # Users who signed up in the last 7 days, split by what happened to their first generations: disjoint subsets
    # of signups_7d (the rest have not tried yet). "So far": an immature cohort, unlike activation_cohort.
    ("new_signups_generated_7d", "new_signups_generated", "7d", None),
    ("new_signups_failed_only_7d", "new_signups_failed_only", "7d", None),
]


def source_time(funnel: dict) -> datetime | None:
    """The upstream snapshot time, never the poll time; naive timestamps are untrustworthy."""
    raw = funnel.get("generated_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


MAX_COUNT = 10**12   # far above any real count, well inside Numeric(18, 4)
RATE_TOLERANCE = 0.001   # the source rounds its rate; a gap wider than this is a contradiction
RATE_FIELD = "activation_rate_7d"
# (smaller, larger): the first can never exceed the second when both are reported. Only
# relations that hold by definition: nested signup windows, a cohort's activated subset,
# and subsets of all users.
ORDERED = [("signups_1d", "signups_7d"), ("signups_7d", "signups_30d"), ("activated_7d", "activation_cohort"),
           ("paid_users", "total_users"), ("active_users_7d", "total_users"), ("checkout_started_7d", "total_users"),
           ("new_signups_generated_7d", "signups_7d"), ("new_signups_failed_only_7d", "signups_7d")]


def _invalid(field: str, raw) -> str | None:
    """Why a reported value cannot be trusted; None when valid. Never echoes the value."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return f"{field}: not a number ({type(raw).__name__})"
    if isinstance(raw, float) and not isfinite(raw):   # isfinite(int) overflows on huge ints; those fail the range checks
        return f"{field}: not finite"
    if field == RATE_FIELD:
        return None if 0 <= raw <= 1 else f"{field}: outside 0..1"
    if raw < 0 or raw != int(raw) or raw > MAX_COUNT:
        return f"{field}: not a non-negative whole count"
    return None


def validate(funnel: dict) -> list[str]:
    """Problems that make the whole snapshot untrustworthy. A field that is absent or null is
    missing, never zero; a present field that is invalid or contradicts another rejects the snapshot,
    so no half-believed funnel reaches a goal."""
    problems = [why for field, *_ in FIELDS if (raw := funnel.get(field)) is not None
                and (why := _invalid(field, raw))]
    if problems:
        return problems
    for small, large in ORDERED:
        a, b = funnel.get(small), funnel.get(large)
        if a is not None and b is not None and a > b:
            problems.append(f"inconsistent: {small} exceeds {large}")
    gen, failed, signups = (funnel.get(k) for k in ("new_signups_generated_7d", "new_signups_failed_only_7d", "signups_7d"))
    if None not in (gen, failed, signups) and gen + failed > signups:   # disjoint subsets cannot sum past their whole
        problems.append("inconsistent: new_signups_generated_7d + new_signups_failed_only_7d exceeds signups_7d")
    done, cohort, rate = funnel.get("activated_7d"), funnel.get("activation_cohort"), funnel.get(RATE_FIELD)
    if None not in (done, cohort, rate) and cohort > 0 and abs(rate - done / cohort) > RATE_TOLERANCE:
        problems.append(f"inconsistent: {RATE_FIELD} disagrees with activated_7d / activation_cohort")
    return problems


def observations(funnel: dict, observed_at: datetime) -> list[schemas.MetricIn]:
    out = []
    for field, metric, window, unit in FIELDS:
        raw = funnel.get(field)
        if raw is None:
            continue
        out.append(schemas.MetricIn(subject=SUBJECT, metric=metric, window=window, value=raw, unit=unit,
                                    source=SOURCE, observed_at=observed_at,
                                    evidence={"endpoint": "/internal/funnel", "seerxo_field": field,
                                              "generated_at": funnel.get("generated_at")}))
    return out


def _rejected(why: list[str]) -> dict:
    return {"inserted": 0, "unchanged": 0, "rejected": why, "note": "Seerxo snapshot rejected; nothing recorded"}


def sync(db: Session, token: str, url: str = DEFAULT_URL, now: datetime | None = None,
         client: httpx.Client | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    with client or httpx.Client(timeout=30) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:  # never include the body: it is upstream text
        return _rejected(["upstream response is not JSON"])
    funnel = body.get("funnel") if isinstance(body, dict) else None
    if not isinstance(funnel, dict):
        return _rejected(["upstream response is not a funnel object"])
    observed_at = source_time(funnel)
    if observed_at is None:
        return {"inserted": 0, "unchanged": 0, "rejected": ["source timestamp missing or invalid"],
                "note": "Seerxo source timestamp missing or invalid"}
    if problems := validate(funnel):
        return _rejected(problems)
    obs = observations(funnel, observed_at)
    if not obs:
        return {"inserted": 0, "unchanged": 0, "rejected": [], "note": "Seerxo returned no fields"}
    return metrics_store.record(db, schemas.MetricsIn(business_unit=UNIT_KEY, observations=obs), now)


def main() -> int:
    token = os.environ.get("SEERXO_FUNNEL_TOKEN", "")
    if not token:
        print("seerxo-funnel: SEERXO_FUNNEL_TOKEN not set; nothing recorded", file=sys.stderr)
        return 2
    from app.db import SessionLocal
    try:
        with SessionLocal() as db:
            result = sync(db, token, os.environ.get("SEERXO_FUNNEL_URL") or DEFAULT_URL)
            print("seerxo-funnel:", result)
            if result.get("rejected"):  # reasons name fields only, never upstream values
                print("seerxo-funnel: snapshot rejected:", "; ".join(result["rejected"]), file=sys.stderr)
                return 1
    except httpx.HTTPStatusError as e:  # status only
        print(f"seerxo-funnel: Seerxo returned HTTP {e.response.status_code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
