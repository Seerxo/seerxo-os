"""Did an improvement work? HYPOTHESIS -> BASELINE -> CHANGE -> OBSERVATION -> EVALUATION -> DECISION -> LEARNING.

The evidence is two existing metric_observations rows of the exact same series (subject, metric, window,
source, unit): one before the change, one after the measurement window. The verdict is arithmetic on those
two numbers. It is an *observed difference*, never a causal claim, and no model's opinion is evidence.
Missing, mismatched or stale data refuses; it never turns into a success.

Nothing here deploys, merges, publishes, spends or edits goals. Founder-only steps (READY, decision) need a
founder identity; the evaluation cannot be decided by whoever evaluated it. An experiment tied to a goal keeps a
snapshot of the goal's selector: if the goal is repointed afterwards, READY, start and acceptance refuse, so a
result is never accepted as evidence for a goal it did not measure. No control group exists, so nothing here is
ever "proven": the evidence says so explicitly. A result becomes organizational
knowledge only as a CANDIDATE Learning that the existing review must approve. Callers commit; each transition
locks the row, so a duplicate call is a no-op instead of a second write.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, enums, goals, keys, metric_sources, metrics_store, models

S = enums.ExperimentStatus
NEXT = {
    S.PROPOSED: {S.READY, S.CANCELLED},
    S.READY: {S.RUNNING, S.CANCELLED},
    S.RUNNING: {S.AWAITING_MEASUREMENT, S.CANCELLED},
    S.AWAITING_MEASUREMENT: {S.EVALUATED, S.CANCELLED},
    S.EVALUATED: {S.ACCEPTED, S.REJECTED},
    S.ACCEPTED: set(), S.REJECTED: set(), S.CANCELLED: set(),
}
SELECTOR_FIELDS = ("subject", "metric", "window", "source", "unit")
SYSTEM = "system:experiments"
OPEN = (S.PROPOSED, S.READY, S.RUNNING, S.AWAITING_MEASUREMENT, S.EVALUATED)   # mirrors uq_experiments_open_fingerprint
MAX_ACTOR = 100   # experiments.created_by / approved_by / decided_by are String(100): refused, never truncated
# The verified-principal id the API layer derives from a Cloudflare Access assertion (docs/APPROVAL_IDENTITY.md):
# founder:access:<lowercase email>. This is a format guard, not authentication: the caller must pass
# Principal.id from require_founder(request), never a request field.
_FOUNDER = re.compile(r"founder:access:[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")


class ExperimentError(ValueError):
    """The request breaks a rule; nothing was changed."""


class InsufficientEvidence(ExperimentError):
    """The measurement cannot be judged. `temporary` means more data may still arrive (nothing is wrong yet);
    otherwise the evidence is permanently unusable. `reason` is a stable code for callers such as the scheduler."""

    def __init__(self, reason: str, message: str, temporary: bool = False):
        super().__init__(message)
        self.reason, self.temporary = reason, temporary


class NotFounder(PermissionError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_founder(actor: str | None) -> bool:
    """Exactly `founder:access:<email>`. Operators (`operator:*`), services (`service:*`), the legacy strings
    `founder` / `founder (...)` and anything merely starting with "founder" are not founder authority."""
    return isinstance(actor, str) and len(actor) <= MAX_ACTOR and _FOUNDER.fullmatch(actor) is not None


def _actor(actor: str | None) -> str:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > MAX_ACTOR or actor != actor.strip() \
            or any(ord(c) < 32 for c in actor):
        raise ExperimentError(f"actor must be a 1-{MAX_ACTOR} character identity without control characters")
    return actor


def _need_founder(actor: str | None) -> None:
    if not is_founder(actor):
        raise NotFounder("only the founder may do this")


def _lock(db: Session, exp: models.Experiment) -> models.Experiment:
    locked = db.scalar(select(models.Experiment).where(models.Experiment.id == exp.id)
                       .with_for_update().execution_options(populate_existing=True))
    if locked is None:
        raise ExperimentError(f"experiment {exp.key or exp.id} not found")
    return locked


def _allow(exp: models.Experiment, to: S) -> None:
    if to not in NEXT[exp.status]:
        raise ExperimentError(f"{exp.key}: {exp.status.value} -> {to.value} is not allowed")


def _move(db: Session, exp: models.Experiment, to: S, actor: str, reason: str | None = None, **after) -> None:
    _allow(exp, to)
    before = {"status": exp.status.value}
    exp.status = to
    audit.record(db, actor=actor, action=f"experiment.{to.value.lower()}", entity_type="experiment",
                 entity_key=exp.key, reason=reason, before=before, after={"status": to.value, **after})


def _selector(raw: dict, unit_key: str) -> dict:
    sel = {k: raw.get(k) for k in SELECTOR_FIELDS}
    missing = [k for k in ("subject", "metric", "source", "unit") if not sel[k]]
    if missing or sel["window"] is None:
        raise ExperimentError(f"metric selector needs {', '.join(missing + (['window'] if sel['window'] is None else []))}")
    if sel["source"] not in metric_sources.SOURCES:
        raise ExperimentError(f"unknown metric source {sel['source']!r}")
    if unit_key not in metric_sources.SOURCES[sel["source"]]["units"]:
        raise ExperimentError(f"source {sel['source']!r} does not report for business unit {unit_key!r}")
    return sel


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _fingerprint(unit_key: str, sel: dict, **parts) -> str:
    body = {"unit": unit_key, "selector": sel, **parts}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class _GoalProblem(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _goal_check(db: Session, goal: models.Goal, sel: dict, unit_id: int, project_id: int | None,
                now: datetime, *, snapshot: dict | None = None, check_closed: bool = True) -> str:
    """Raises _GoalProblem unless the goal still measures exactly this experiment's series. Returns the match mode.

    Uses goals.resolve, the single metric-selection algorithm, for a source-less goal selector: it counts as the
    experiment's source only while it actually resolves to it (one reporter), never by silently ignoring `source`."""
    if snapshot is not None and goal.metric_selector != snapshot:
        raise _GoalProblem("GOAL_SELECTOR_CHANGED", "goal selector changed since the experiment was proposed")
    if check_closed and goal.status in goals.CLOSED:
        raise _GoalProblem("GOAL_CLOSED", f"goal {goal.id} is closed ({goal.status.value})")
    project = db.get(models.Project, goal.project_id) if goal.project_id else None
    if project is None:
        raise _GoalProblem("GOAL_UNIT_UNKNOWN", f"goal {goal.id} has no project, so its business unit is unknown")
    if project.business_unit_id != unit_id:
        raise _GoalProblem("GOAL_UNIT_MISMATCH", f"goal {goal.id} belongs to another business unit")
    if project_id is not None and project_id != project.id:
        raise _GoalProblem("GOAL_PROJECT_MISMATCH", f"goal {goal.id} belongs to another project")
    gs = goal.metric_selector
    if not goals.selector_valid(gs):
        raise _GoalProblem("GOAL_SELECTOR_INVALID", f"goal {goal.id} metric selector is invalid")
    if any(gs[k] != sel[k] for k in ("subject", "metric")) or gs.get("window", "") != sel["window"]:
        raise _GoalProblem("GOAL_METRIC_MISMATCH", f"goal {goal.id} measures a different series")
    if gs.get("source") is not None:
        if gs["source"] != sel["source"]:
            raise _GoalProblem("GOAL_SOURCE_MISMATCH", f"goal {goal.id} measures a different source")
        return "EXACT_SOURCE"
    row, reason = goals.resolve(db, gs, now)
    if row is None or row.source != sel["source"]:
        raise _GoalProblem("GOAL_SOURCE_UNVERIFIED",
                           f"goal {goal.id} selector has no source and does not resolve to source {sel['source']!r} "
                           f"({reason}); pin the source on the goal")
    return "SOURCELESS_RESOLVED"


def goal_link(db: Session, exp: models.Experiment, now: datetime | None = None, check_closed: bool = False) -> dict:
    """The experiment's goal link as evidence: LINKED (with how it matched), NO_GOAL, or the reason it is broken."""
    if exp.goal_id is None:
        return {"status": "NO_GOAL"}
    goal = db.get(models.Goal, exp.goal_id)
    if goal is None:
        return {"status": "GOAL_MISSING", "goal_id": exp.goal_id}
    try:
        mode = _goal_check(db, goal, exp.metric_selector, exp.business_unit_id, exp.project_id,
                           now or datetime.now(timezone.utc), snapshot=exp.goal_selector_snapshot,
                           check_closed=check_closed)
    except _GoalProblem as p:
        return {"status": p.code, "goal_id": goal.id, "detail": str(p)}
    return {"status": "LINKED", "goal_id": goal.id, "mode": mode}


def _require_goal_link(db: Session, exp: models.Experiment, now: datetime, check_closed: bool) -> None:
    if exp.goal_id is None:
        return
    goal = db.get(models.Goal, exp.goal_id)
    if goal is None:
        raise ExperimentError(f"{exp.key}: goal {exp.goal_id} no longer exists")
    try:
        _goal_check(db, goal, exp.metric_selector, exp.business_unit_id, exp.project_id, now,
                    snapshot=exp.goal_selector_snapshot, check_closed=check_closed)
    except _GoalProblem as p:
        raise ExperimentError(f"{exp.key}: {p}") from p


def _require_identity(db: Session, exp: models.Experiment) -> None:
    """The metric identity is still one the source registry accepts for this business unit."""
    unit = db.get(models.BusinessUnit, exp.business_unit_id)
    _selector(exp.metric_selector, unit.key)


def _in_series(exp: models.Experiment, row: models.MetricObservation) -> bool:
    sel = exp.metric_selector
    return (row.business_unit_id == exp.business_unit_id and row.subject == sel["subject"]
            and row.metric == sel["metric"] and row.window == sel["window"] and row.source == sel["source"]
            and row.unit == sel["unit"])


def propose(db: Session, *, title: str, hypothesis: str, business_unit: str, metric_selector: dict,
            expected_direction: str, measurement_window_hours: int, actor: str, min_effect: float = 0,
            goal_id: int | None = None, project_id: int | None = None,
            now: datetime | None = None) -> models.Experiment:
    """A repeated identical proposal returns the open experiment instead of creating a second one."""
    now = now or datetime.now(timezone.utc)
    _actor(actor)
    if not title.strip() or not hypothesis.strip():
        raise ExperimentError("title and hypothesis are required")
    if expected_direction not in ("UP", "DOWN"):
        raise ExperimentError("expected_direction is UP or DOWN")
    if not isinstance(measurement_window_hours, int) or isinstance(measurement_window_hours, bool) \
            or measurement_window_hours <= 0:
        raise ExperimentError("measurement_window_hours must be a positive integer")
    if min_effect < 0:
        raise ExperimentError("min_effect cannot be negative")
    unit = db.scalar(select(models.BusinessUnit).where(models.BusinessUnit.key == business_unit))
    if unit is None:
        raise ExperimentError(f"unknown business unit {business_unit!r}")
    sel = _selector(metric_selector, unit.key)
    snapshot = None
    if goal_id is not None:
        goal = db.get(models.Goal, goal_id)
        if goal is None:
            raise ExperimentError(f"unknown goal {goal_id}")
        try:
            _goal_check(db, goal, sel, unit.id, project_id, now)
        except _GoalProblem as p:
            raise ExperimentError(str(p)) from p
        snapshot = json.loads(json.dumps(goal.metric_selector))
    fingerprint = _fingerprint(unit.key, sel, goal=goal_id, project=project_id, direction=expected_direction,
                               window=measurement_window_hours, min_effect=float(min_effect),
                               title=_norm(title), hypothesis=_norm(hypothesis))

    def open_one():
        return db.scalar(select(models.Experiment).where(models.Experiment.fingerprint == fingerprint,
                                                         models.Experiment.status.in_(OPEN)))
    if (existing := open_one()) is not None:
        return existing
    exp = models.Experiment(
        title=title.strip(), hypothesis=hypothesis.strip(), status=S.PROPOSED, goal_id=goal_id,
        business_unit_id=unit.id, project_id=project_id, metric_selector=sel,
        expected_direction=expected_direction, min_effect=min_effect,
        measurement_window_hours=measurement_window_hours, created_by=actor,
        goal_selector_snapshot=snapshot, fingerprint=fingerprint)
    try:
        keys.add_with_key(db, exp, "EXP")
    except IntegrityError as e:   # a concurrent identical proposal won the unique index
        if "fingerprint" not in str(e.orig) and "uq_experiments_open_fingerprint" not in str(e.orig):
            raise
        if (existing := open_one()) is None:
            raise
        return existing
    audit.record(db, actor=actor, action="experiment.proposed", entity_type="experiment", entity_key=exp.key,
                 before=None, after={"status": "PROPOSED", "selector": sel, "goal_id": goal_id})
    return exp


def _series(exp: models.Experiment):
    sel = exp.metric_selector
    q = select(models.MetricObservation).where(
        models.MetricObservation.business_unit_id == exp.business_unit_id,
        models.MetricObservation.subject == sel["subject"], models.MetricObservation.metric == sel["metric"],
        models.MetricObservation.window == sel["window"], models.MetricObservation.source == sel["source"])
    return q


def _check_unit(exp: models.Experiment, row: models.MetricObservation, what: str) -> None:
    if row.unit != exp.metric_selector["unit"]:
        raise InsufficientEvidence("UNIT_MISMATCH", f"{what} observation is in unit {row.unit!r}, "
                                   f"expected {exp.metric_selector['unit']!r}")


def approve_ready(db: Session, exp: models.Experiment, founder: str, now: datetime | None = None) -> models.Experiment:
    """PROPOSED -> READY. The founder accepts the plan, and a fresh, correctly typed baseline is pinned."""
    _need_founder(founder)
    now = now or datetime.now(timezone.utc)
    exp = _lock(db, exp)
    if exp.status == S.READY:
        return exp
    _allow(exp, S.READY)
    _require_goal_link(db, exp, now, check_closed=True)
    _require_identity(db, exp)
    base = db.scalars(_series(exp).where(models.MetricObservation.observed_at <= now)
                      .order_by(models.MetricObservation.observed_at.desc()).limit(1)).first()
    if base is None:
        raise ExperimentError("no baseline observation of this exact series")
    _check_unit(exp, base, "baseline")
    if now - _aware(base.observed_at) > metrics_store.stale_after(base.source):
        raise ExperimentError("the newest baseline observation is stale for its source")
    exp.baseline_observation_id, exp.approved_by, exp.ready_at = base.id, founder, now
    _move(db, exp, S.READY, founder, baseline_observation_id=base.id)
    return exp


def start(db: Session, exp: models.Experiment, intervention_ref: str, actor: str,
          now: datetime | None = None) -> models.Experiment:
    """READY -> RUNNING: the change has been made (by someone else, through its own approvals)."""
    _actor(actor)
    now = now or datetime.now(timezone.utc)
    exp = _lock(db, exp)
    if exp.status == S.RUNNING and exp.intervention_ref == intervention_ref:
        return exp
    _allow(exp, S.RUNNING)
    if not (intervention_ref or "").strip():
        raise ExperimentError("an intervention reference (task, PR, commit) is required")
    base = db.get(models.MetricObservation, exp.baseline_observation_id) if exp.baseline_observation_id else None
    if base is None or _aware(base.observed_at) > now:
        raise ExperimentError("the baseline must be observed before the change")
    if not _in_series(exp, base):
        raise ExperimentError("the baseline observation is not in this experiment's series")
    if now - _aware(base.observed_at) > metrics_store.stale_after(base.source):
        raise ExperimentError("the baseline observation is stale for its source; re-approve with a fresh baseline")
    _require_goal_link(db, exp, now, check_closed=True)
    _require_identity(db, exp)
    exp.intervention_ref, exp.started_at = intervention_ref.strip(), now
    exp.measure_after = now + timedelta(hours=exp.measurement_window_hours)
    _move(db, exp, S.RUNNING, actor, intervention_ref=exp.intervention_ref, measure_after=exp.measure_after.isoformat())
    return exp


def close_window(db: Session, exp: models.Experiment, actor: str = SYSTEM,
                 now: datetime | None = None) -> models.Experiment:
    """RUNNING -> AWAITING_MEASUREMENT, only once the measurement window has fully elapsed."""
    _actor(actor)
    now = now or datetime.now(timezone.utc)
    exp = _lock(db, exp)
    if exp.status == S.AWAITING_MEASUREMENT:
        return exp
    if exp.status == S.RUNNING and now < _aware(exp.measure_after):
        raise ExperimentError(f"{exp.key}: the measurement window ends {_aware(exp.measure_after).isoformat()}")
    _move(db, exp, S.AWAITING_MEASUREMENT, actor)
    return exp


def _ref(row: models.MetricObservation) -> dict:
    return {"observation_id": row.id, "value": float(row.value), "unit": row.unit, "source": row.source,
            "observed_at": _aware(row.observed_at).isoformat()}


def evaluate(db: Session, exp: models.Experiment, now: datetime | None = None) -> models.Experiment:
    """AWAITING_MEASUREMENT -> EVALUATED from the first reading taken after the window. Idempotent."""
    now = now or datetime.now(timezone.utc)
    exp = _lock(db, exp)
    if exp.status == S.EVALUATED:
        return exp
    if exp.status != S.AWAITING_MEASUREMENT:
        raise ExperimentError(f"{exp.key}: {exp.status.value} -> EVALUATED is not allowed")
    result = db.scalars(_series(exp).where(models.MetricObservation.observed_at >= exp.measure_after,
                                           models.MetricObservation.observed_at <= now)
                        .order_by(models.MetricObservation.observed_at, models.MetricObservation.id).limit(1)).first()
    if result is None:
        raise InsufficientEvidence("NO_POST_WINDOW_OBSERVATION", "insufficient evidence: no observation of this "
                                   "series after the measurement window", temporary=True)
    _check_unit(exp, result, "result")
    base = db.get(models.MetricObservation, exp.baseline_observation_id)
    if base is None or not _in_series(exp, base) or not _in_series(exp, result):
        raise InsufficientEvidence("SERIES_MISMATCH", "insufficient evidence: an observation is not in this "
                                   "experiment's series")
    if _aware(result.observed_at) - _aware(exp.measure_after) > metrics_store.stale_after(result.source):
        raise InsufficientEvidence("LATE_FIRST_OBSERVATION", "insufficient evidence: the first reading after the "
                                   "window came too late to represent it (the source went silent longer than its "
                                   "freshness window)")
    before, after = float(base.value), float(result.value)
    delta = after - before
    signed = delta if exp.expected_direction == "UP" else -delta
    threshold = float(exp.min_effect)
    outcome = ("IMPROVED" if signed > 0 and signed >= threshold else
               "WORSENED" if signed < 0 and -signed >= threshold else "NO_MEANINGFUL_CHANGE")
    exp.result_observation_id, exp.outcome, exp.evaluated_at = result.id, outcome, now
    exp.evidence = {"baseline": _ref(base), "result": _ref(result), "delta": round(delta, 4),
                    "delta_pct": round(delta / before * 100, 2) if before else None,
                    "expected_direction": exp.expected_direction, "min_effect": threshold,
                    "window_hours": exp.measurement_window_hours, "evaluated_by": SYSTEM,
                    "claim": "OBSERVED_DIFFERENCE", "causality": "NOT_ESTABLISHED (no control group)",
                    "control_group": None, "statistically_proven": False,
                    "goal_link": goal_link(db, exp, now)}
    _move(db, exp, S.EVALUATED, SYSTEM, outcome=outcome, delta=exp.evidence["delta"])
    return exp


def decide(db: Session, exp: models.Experiment, accept: bool, founder: str, reason: str,
           now: datetime | None = None) -> models.Experiment:
    """EVALUATED -> ACCEPTED | REJECTED, by the founder, who may not be the evaluator."""
    _need_founder(founder)
    now = now or datetime.now(timezone.utc)
    exp = _lock(db, exp)
    target = S.ACCEPTED if accept else S.REJECTED
    if exp.status == target and exp.decided_by == founder:
        return exp
    _allow(exp, target)
    if not (reason or "").strip():
        raise ExperimentError("a decision needs a reason")
    if founder == (exp.evidence or {}).get("evaluated_by"):
        raise ExperimentError("an experiment cannot approve its own result")
    if accept:   # rejecting is always safe; accepting must still point at the goal it measured
        _require_goal_link(db, exp, now, check_closed=False)
    exp.decided_by, exp.decided_at, exp.decision_reason = founder, now, reason.strip()
    _move(db, exp, target, founder, reason.strip(), outcome=exp.outcome,
          result_observation_id=exp.result_observation_id)
    return exp


def cancel(db: Session, exp: models.Experiment, actor: str, reason: str) -> models.Experiment:
    _actor(actor)
    exp = _lock(db, exp)
    if exp.status == S.CANCELLED:
        return exp
    if not (reason or "").strip():
        raise ExperimentError("cancelling needs a reason")
    _move(db, exp, S.CANCELLED, actor, reason.strip())
    return exp


def propose_learning(db: Session, exp: models.Experiment, actor: str) -> models.Learning:
    """A CANDIDATE Learning tied to the evidence. Never APPROVED here: the existing review decides."""
    _actor(actor)
    exp = _lock(db, exp)
    if exp.learning_id:
        return db.get(models.Learning, exp.learning_id)
    if exp.status not in (S.ACCEPTED, S.REJECTED):
        raise ExperimentError("only a decided experiment can propose a learning")
    ev = exp.evidence
    learning = models.Learning(
        title=f"Experiment {exp.key}: {exp.title}"[:300],
        lesson=(f"Hypothesis: {exp.hypothesis} Observed {ev['baseline']['value']} -> {ev['result']['value']} "
                f"{exp.metric_selector['unit']} ({exp.outcome}); decision {exp.status.value}: {exp.decision_reason}. "
                f"Observed difference only; causality not established."),
        evidence=f"{exp.key} observations {ev['baseline']['observation_id']} and {ev['result']['observation_id']}",
        business_unit_id=exp.business_unit_id, project_id=exp.project_id, tags="experiment",
        created_by=actor, status=enums.LearningStatus.CANDIDATE)
    db.add(learning)
    db.flush()
    exp.learning_id = learning.id
    audit.record(db, actor=actor, action="experiment.learning_candidate", entity_type="experiment",
                 entity_key=exp.key, after={"learning_id": learning.id, "status": "CANDIDATE"})
    return learning


def view(exp: models.Experiment, now: datetime | None = None) -> dict:
    """Freshness is computed from the baseline/result timestamps and the source policy, never stored."""
    now = now or datetime.now(timezone.utc)
    ev = exp.evidence or {}
    return {"key": exp.key, "title": exp.title, "hypothesis": exp.hypothesis, "status": exp.status.value,
            "goal_id": exp.goal_id, "metric_selector": exp.metric_selector,
            "expected_direction": exp.expected_direction, "min_effect": float(exp.min_effect),
            "measurement_window_hours": exp.measurement_window_hours, "intervention_ref": exp.intervention_ref,
            "measure_after": _aware(exp.measure_after).isoformat() if exp.measure_after else None,
            "outcome": exp.outcome, "evidence": exp.evidence or None, "approved_by": exp.approved_by,
            "decided_by": exp.decided_by, "decision_reason": exp.decision_reason,
            "learning_id": exp.learning_id,
            "result_freshness": (None if not ev else "STALE" if now - datetime.fromisoformat(
                ev["result"]["observed_at"]) > metrics_store.stale_after(ev["result"]["source"]) else "VERIFIED")}
