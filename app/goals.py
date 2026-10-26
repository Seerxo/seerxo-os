"""Operational goals (§30): a goal is measured by one metric observation, never by opinion.

Progress = (latest verified value - baseline) / (target - baseline), from the newest
metric_observations row matching the goal's selector. No selector, no observation or a
non-numeric baseline/target means progress is unknown (None), never 0.
"""
from datetime import datetime, timezone
from math import isfinite

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, metrics_store, models

CLOSED = (enums.GoalStatus.ACHIEVED, enums.GoalStatus.MISSED, enums.GoalStatus.ABANDONED)


class GoalNotMeasurable(ValueError):
    """Activating a goal that has no metric selector."""


def number(text: str | None) -> float | None:
    """'12,400' -> 12400.0; anything that is not a plain number -> None."""
    if text is None:
        return None
    try:
        value = float(str(text).replace(",", "").replace("_", "").strip())
    except ValueError:
        return None
    return value if isfinite(value) else None


def selector_valid(selector) -> bool:
    """A usable persisted selector: subject and metric strings, optional string window and source."""
    if not isinstance(selector, dict):
        return False
    window, source = selector.get("window", ""), selector.get("source")
    return (all(isinstance(selector.get(k), str) and selector[k] for k in ("subject", "metric"))
            and isinstance(window, str) and (source is None or (isinstance(source, str) and bool(source))))


def config_issues(goal: models.Goal) -> list[str]:
    """What is wrong with the goal itself, whatever the metric currently reads (empty = evaluable).

    Codes: SELECTOR_INVALID, BASELINE_INVALID, TARGET_INVALID, TARGET_EQUALS_BASELINE.
    """
    base, target = number(goal.baseline), number(goal.target)
    issues = [] if selector_valid(goal.metric_selector) else ["SELECTOR_INVALID"]
    if base is None:
        issues.append("BASELINE_INVALID")
    if target is None:
        issues.append("TARGET_INVALID")
    if base is not None and base == target:
        issues.append("TARGET_EQUALS_BASELINE")
    return issues


def resolve(db: Session, selector: dict | None, now: datetime | None = None) -> tuple[models.MetricObservation | None, str]:
    """The one metric-selection algorithm: (row, reason_code). A row only with MEASUREMENT_VERIFIED.

    Reason codes: SELECTOR_INVALID, MEASUREMENT_MISSING, FUTURE_OBSERVATION, SOURCE_AMBIGUOUS,
    UNIT_CONFLICT, MEASUREMENT_VERIFIED. Freshness and numeric validity are judged by `assess`.
    """
    if not selector_valid(selector):
        return None, "SELECTOR_INVALID"
    subject, metric, source = (selector.get(k) for k in ("subject", "metric", "source"))
    window = selector.get("window", "")
    now = metrics_store._aware(now or datetime.now(timezone.utc))
    M = models.MetricObservation
    series = (M.subject == subject, M.metric == metric, M.window == window)
    if source is not None:
        series += (M.source == source,)
    if db.scalars(select(M.id).where(*series).limit(1)).first() is None:
        return None, "MEASUREMENT_MISSING"
    series += (M.observed_at <= now,)  # ingestion tolerates clock skew; a future reading is no evidence
    if db.scalars(select(M.id).where(*series).limit(1)).first() is None:
        return None, "FUTURE_OBSERVATION"
    if source is None:
        # A source-less selector is safe only while one reporter exists. Choosing its newest
        # row after a second reporter appears would silently make that reporter authoritative.
        sources = db.scalars(select(M.source).where(*series).distinct().limit(2)).all()
        if len(sources) != 1:
            return None, "SOURCE_AMBIGUOUS"
        series += (M.source == sources[0],)
    if len(db.scalars(select(M.unit).where(*series).distinct().limit(2)).all()) != 1:
        return None, "UNIT_CONFLICT"
    return db.scalars(select(M).where(*series).order_by(M.observed_at.desc()).limit(1)).first(), "MEASUREMENT_VERIFIED"


def latest(db: Session, selector: dict | None, now: datetime | None = None) -> models.MetricObservation | None:
    """One current observation for a valid, unambiguous selector; otherwise None."""
    return resolve(db, selector, now)[0]


def assess(db: Session, goal: models.Goal, now: datetime | None = None) -> dict:
    """Every fact about a goal's measurement, with the reason it is or is not usable.

    `progress` and app.goal_evidence both read this, so they cannot disagree.
    reason: MEASUREMENT_VERIFIED | MEASUREMENT_STALE | INVALID_NUMERIC_EVIDENCE | the `resolve` codes.
    progress_reason: PROGRESS_COMPUTED | BASELINE_INVALID | TARGET_INVALID | TARGET_EQUALS_BASELINE | None
    (None while no usable value exists).
    """
    now = now or datetime.now(timezone.utc)
    row, reason = resolve(db, goal.metric_selector, now)
    base, target = number(goal.baseline), number(goal.target)
    out = {"selector": goal.metric_selector, "baseline_value": base, "target_value": target,
           "latest": None, "freshness": "UNAVAILABLE", "progress": None, "reason": reason,
           "progress_reason": None, "observation_id": None}
    if row is None:
        return out
    observed = metrics_store._aware(row.observed_at)
    try:
        value = float(row.value)
    except (TypeError, ValueError, OverflowError):
        value = float("nan")
    if not isfinite(value):
        out["reason"] = "INVALID_NUMERIC_EVIDENCE"
        return out
    out["latest"] = {"value": value, "unit": row.unit, "source": row.source,
                     "observed_at": observed.isoformat()}
    out["observation_id"] = row.id
    stale = now - observed > metrics_store.stale_after(row.source)
    out["freshness"], out["reason"] = ("STALE", "MEASUREMENT_STALE") if stale else ("VERIFIED", reason)
    if base is None:
        out["progress_reason"] = "BASELINE_INVALID"
    elif target is None:
        out["progress_reason"] = "TARGET_INVALID"
    elif target == base:
        out["progress_reason"] = "TARGET_EQUALS_BASELINE"
    else:
        out["progress"] = round((value - base) / (target - base), 4)
        out["progress_reason"] = "PROGRESS_COMPUTED"
    return out


def progress(db: Session, goal: models.Goal, now: datetime | None = None) -> dict:
    a = assess(db, goal, now)
    return {k: a[k] for k in ("selector", "baseline_value", "target_value", "latest", "freshness", "progress")}


def view(db: Session, goal: models.Goal, now: datetime | None = None) -> dict:
    project = db.get(models.Project, goal.project_id) if goal.project_id else None
    return {"id": goal.id, "title": goal.title, "description": goal.description,
            "metric": goal.metric, "baseline": goal.baseline, "target": goal.target,
            "deadline": goal.deadline.isoformat() if goal.deadline else None,
            "priority": goal.priority.value, "status": goal.status.value,
            "project": project.key if project else None, **progress(db, goal, now)}


def check_activation(goal: models.Goal) -> None:
    if goal.status == enums.GoalStatus.ACTIVE and not goal.metric_selector:
        raise GoalNotMeasurable("an ACTIVE goal needs a metric selector (§30: a goal without a metric is not a goal)")
