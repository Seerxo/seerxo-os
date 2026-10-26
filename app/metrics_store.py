"""Business measurements with provenance (§102). Executors report facts; Company OS keeps them.

No value is ever invented: a metric nobody reported has no row, and the Desk says
"no verified source" instead of 0.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import metric_sources, models, schemas

FUTURE_SKEW = timedelta(minutes=5)
# A business number older than its source's window (app/metric_sources.py) is shown as
# STALE, not as current truth. A source not registered there gets the default.
STALE_AFTER = metric_sources.DEFAULT_FRESHNESS


def stale_after(source: str | None) -> timedelta:
    return metric_sources.freshness(source)


class UnknownBusinessUnit(LookupError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _freshness(observed_at: datetime, source: str | None, now: datetime) -> str:
    return "STALE" if now - _aware(observed_at) > stale_after(source) else "VERIFIED"


def record(db: Session, body: schemas.MetricsIn, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    unit = db.execute(select(models.BusinessUnit).where(
        models.BusinessUnit.key == body.business_unit)).scalar_one_or_none()
    if unit is None:
        raise UnknownBusinessUnit(body.business_unit)
    counts = {"inserted": 0, "unchanged": 0, "rejected": []}
    for o in body.observations:
        observed = _aware(o.observed_at)
        if observed > now + FUTURE_SKEW:
            counts["rejected"].append(f"{o.subject}/{o.metric}: observed_at is in the future")
            continue
        row = models.MetricObservation(business_unit_id=unit.id, subject=o.subject, metric=o.metric,
                                       window=o.window, value=o.value, unit=o.unit, source=o.source,
                                       observed_at=observed, evidence=o.evidence)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            counts["inserted"] += 1
        except IntegrityError:
            # Unique-key collisions are idempotent only if the persisted measurement agrees.
            # Other integrity violations must surface, not silently count as a successful retry.
            previous = db.execute(select(models.MetricObservation).where(
                models.MetricObservation.source == o.source,
                models.MetricObservation.subject == o.subject,
                models.MetricObservation.metric == o.metric,
                models.MetricObservation.window == o.window,
                models.MetricObservation.observed_at == observed,
            )).scalar_one_or_none()
            if previous is None:
                raise
            # Numeric(18, 4) rounds inputs at storage; normalize the retry to the
            # same precision before comparing so a genuine retry remains idempotent.
            normalized = Decimal(str(o.value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if (previous.business_unit_id != unit.id or previous.value != normalized
                    or previous.unit != o.unit):
                counts["rejected"].append(
                    f"{o.subject}/{o.metric}: conflicting reading at an existing source timestamp")
            else:
                counts["unchanged"] += 1
    db.commit()
    return counts


REVENUE_SERIES = {"today": ("revenue", "today"), "month": ("revenue", "mtd"), "mrr": ("mrr", ""),
                  "total": ("revenue", "all_time")}


def revenue(db: Session, now: datetime | None = None) -> dict | None:
    """Latest revenue readings with per-series freshness, or None when no source reported.
    ponytail: Seerxo/Polar is the only revenue source; sum per unit when a second one reports."""
    now = now or datetime.now(timezone.utc)
    out, series, newest = {"unit": None}, {}, None
    for key, (metric, window) in REVENUE_SERIES.items():
        r = db.execute(select(models.MetricObservation).where(
            models.MetricObservation.source == "polar-api", models.MetricObservation.metric == metric,
            models.MetricObservation.window == window)
            .order_by(models.MetricObservation.observed_at.desc()).limit(1)).scalar_one_or_none()
        # A reading belongs to its UTC day/month: yesterday's "today" is not today's revenue.
        period = {"today": "%Y-%m-%d", "month": "%Y-%m"}.get(key)
        if r and period and (_aware(r.observed_at).astimezone(timezone.utc).strftime(period)
                             != now.astimezone(timezone.utc).strftime(period)):
            r = None
        out[key] = float(r.value) if r else None
        series[key] = ({"source": r.source, "observed_at": _aware(r.observed_at).isoformat(),
                        "freshness": _freshness(r.observed_at, r.source, now)} if r else None)
        if r:
            at = _aware(r.observed_at)
            newest = max(newest or at, at)
            out["unit"] = r.unit
    if newest is None:
        return None
    return {**out, "series": series, "source": "polar-api", "observed_at": newest.isoformat(),
            "freshness": "STALE" if any(s and s["freshness"] == "STALE" for s in series.values())
            else "VERIFIED"}


def portfolio(db: Session, now: datetime | None = None, per_unit: int = 60) -> list[dict]:
    """Per business unit: each latest series and its own freshness."""
    now = now or datetime.now(timezone.utc)
    out = []
    for unit in db.execute(select(models.BusinessUnit).order_by(
            models.BusinessUnit.priority, models.BusinessUnit.key)).scalars():
        # History is append-only: show each series once, at its newest reading.
        # ponytail: walks the unit's history newest-first; use DISTINCT ON if it grows past ~10^5 rows.
        rows, seen = [], set()
        for r in db.execute(select(models.MetricObservation)
                            .where(models.MetricObservation.business_unit_id == unit.id)
                            .order_by(models.MetricObservation.observed_at.desc())).scalars():
            series = (r.source, r.subject, r.metric, r.window)
            if series not in seen:
                seen.add(series)
                rows.append(r)
                if len(rows) == per_unit:
                    break
        latest = _aware(rows[0].observed_at) if rows else None
        metrics = [{"subject": r.subject, "metric": r.metric, "window": r.window,
                    "value": float(r.value), "unit": r.unit, "source": r.source,
                    "observed_at": _aware(r.observed_at).isoformat(),
                    "freshness": _freshness(r.observed_at, r.source, now),
                    "label": (r.evidence or {}).get("title")} for r in rows]
        out.append({
            "key": unit.key, "name": unit.name, "status": unit.status.value,
            "priority": unit.priority.value,
            "freshness": ("UNAVAILABLE" if not metrics else "STALE"
                          if any(m["freshness"] == "STALE" for m in metrics) else "VERIFIED"),
            "latest_at": latest.isoformat() if latest else None,
            "metrics": metrics,
        })
    return out


# ponytail: one series' history is capped so a long-lived YouTube video cannot make a single response huge; raise it when a chart needs more.
SERIES_MAX_POINTS = 1000


def series(db: Session, *, source: str, subject: str, metric: str, window: str = "", days: int = 30,
           now: datetime | None = None) -> dict:
    """Every reading of one series in the last `days` days, oldest first, for a trend chart.
    No reading is an empty `points` list, never a zero: the chart must say "no data" instead of drawing a line."""
    now = now or datetime.now(timezone.utc)
    rows = db.execute(select(models.MetricObservation).where(
        models.MetricObservation.source == source,
        models.MetricObservation.subject == subject,
        models.MetricObservation.metric == metric,
        models.MetricObservation.window == window,
        models.MetricObservation.observed_at >= now - timedelta(days=days),
        models.MetricObservation.observed_at <= now + FUTURE_SKEW,
    ).order_by(models.MetricObservation.observed_at).limit(SERIES_MAX_POINTS)).scalars().all()
    return {
        "source": source, "subject": subject, "metric": metric, "window": window, "days": days,
        "unit": rows[-1].unit if rows else None,
        "points": [{"observed_at": _aware(r.observed_at).isoformat(), "value": float(r.value)} for r in rows],
    }
