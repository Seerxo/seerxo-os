"""Experiment scheduler tick (A2-004): advance experiments whose measurement window is over, nothing more.

    RUNNING + window elapsed            -> experiments.close_window()  -> AWAITING_MEASUREMENT
    AWAITING_MEASUREMENT + valid data   -> experiments.evaluate()      -> EVALUATED (founder reviews it)

All rules live in app/experiments.py; this module only decides which experiments to hand it, in what order and
with what isolation. It never writes a metric observation, calls an external service or model, approves, accepts,
rejects, proposes a learning, or touches goals, tasks, approvals or incidents. Missing data is "waiting", never a
verdict. Not wired to any loop: the caller runs `tick` (docs/EXPERIMENT_SCHEDULER.md).

Concurrency: each experiment row is claimed with FOR UPDATE SKIP LOCKED, so two scheduler runs never process the
same experiment or wait on each other, and each experiment runs in its own SAVEPOINT, so one failure rolls back only
itself. `commit=False` (default) leaves the transaction to the caller; `commit=True` commits after every experiment,
so a crash loses at most the one in flight and a restart resumes (every step is idempotent).

Fairness: candidates are scanned in id order from a rotating cursor (one ControlState row, CURSOR_KEY), wrapping at
the end, and the cursor moves to the last candidate a tick reached. With one scheduler whose cursor saves persist,
each tick passes at least min(limit, SCAN_CAP) candidates (or all of them), so none is starved by experiments that
cannot progress. The cursor is advisory: lost or corrupt, it only restarts the rotation. It is updated with SKIP
LOCKED and created only by a self-committing tick, so a scheduler never waits on an open caller transaction for it
(at most on another tick's INSERT-to-COMMIT step, then it skips the save); concurrent schedulers may move it
backwards, which repeats idempotent work. docs/EXPERIMENT_SCHEDULER.md states exactly what is and is not guaranteed.

Time: a state-changing tick refuses a `now` more than CLOCK_SKEW ahead of the real UTC clock, before touching the
database. Within that tolerance a window can close up to CLOCK_SKEW before it ends by the real clock (the accepted
limit). A past `now` is allowed (it can only find less evidence). Dry runs may simulate any time: they keep nothing.
"""
import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import enums, experiments, metrics_store, models

log = logging.getLogger("company.experiment_scheduler")

ACTOR = "system:experiment-scheduler"
DEFAULT_LIMIT = 50   # experiments advanced per tick
SCAN_CAP = 1000      # candidates examined per tick; the cursor rotates through the rest on later ticks
CURSOR_KEY = "experiment-scheduler-cursor"
CLOCK_SKEW = metrics_store.FUTURE_SKEW   # 5 minutes: the same allowance metric ingestion gives observed_at
MAX_ID = 2 ** 31 - 1   # experiments.id is a 32-bit integer on PostgreSQL; a larger cursor would fail every query
S = enums.ExperimentStatus
WINDOW_CLOSED, EVALUATED, WAITING_FOR_DATA = "WINDOW_CLOSED", "EVALUATED", "WAITING_FOR_DATA"
INSUFFICIENT_EVIDENCE, BLOCKED, ERROR, SKIPPED = "INSUFFICIENT_EVIDENCE", "BLOCKED", "ERROR", "SKIPPED"


def enabled(environ=os.environ) -> bool:
    """Off unless the founder switches it on; the loop that calls tick() must honor this."""
    return environ.get("EXPERIMENT_SCHEDULER_ENABLED", "false").lower() == "true"


def apply_enabled(environ=os.environ) -> bool:
    """Persistent execution needs both switches: EXPERIMENT_SCHEDULER_ENABLED=true and EXPERIMENT_SCHEDULER_APPLY=yes."""
    return enabled(environ) and environ.get("EXPERIMENT_SCHEDULER_APPLY") == "yes"


@dataclass(frozen=True)
class Outcome:
    key: str
    category: str
    reason: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable identity of this state, so a caller can notify on change instead of on every tick."""
        return f"{self.key}:{self.category}:{self.reason or ''}"

    def to_dict(self) -> dict:
        return {"key": self.key, "category": self.category, "reason": self.reason, "detail": self.detail,
                "fingerprint": self.fingerprint}


@dataclass
class TickResult:
    now: str
    limit: int
    dry_run: bool
    outcomes: list[Outcome] = field(default_factory=list)
    transitions: int = 0       # state changes made (closing a window and evaluating are one each)
    advanced: int = 0          # experiments that changed state; `limit` bounds this
    truncated: bool = False    # more work remained than `limit` / SCAN_CAP allowed this tick

    @property
    def counts(self) -> dict:
        out: dict = {}
        for o in self.outcomes:
            out[o.category] = out.get(o.category, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {"now": self.now, "limit": self.limit, "dry_run": self.dry_run, "transitions": self.transitions,
                "advanced": self.advanced,
                "truncated": self.truncated, "counts": self.counts, "outcomes": [o.to_dict() for o in self.outcomes]}


class FutureClock(ValueError):
    """A state-changing tick was given a time ahead of the real clock; nothing was read or written."""


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(now: datetime | None) -> datetime:
    now = now or _real_now()
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _cursor(db: Session) -> int:
    row = db.get(models.ControlState, CURSOR_KEY)
    after = row.value.get("after_id") if row is not None and isinstance(row.value, dict) else None
    return after if isinstance(after, int) and not isinstance(after, bool) and 0 <= after <= MAX_ID else 0


def _save_cursor(db: Session, after_id: int, create: bool) -> None:
    """Best effort, in a SAVEPOINT: another scheduler holding or creating the row simply wins this round.
    `create` only for a tick that commits right after: an INSERT left uncommitted in a caller's open transaction
    would make every other scheduler's INSERT of the same key wait for that transaction to end."""
    try:
        with db.begin_nested():
            if db.scalar(select(models.ControlState.key).where(models.ControlState.key == CURSOR_KEY)) is None:
                if create:
                    db.add(models.ControlState(key=CURSOR_KEY, value={"after_id": after_id}, updated_by=ACTOR))
            else:
                row = db.scalar(select(models.ControlState).where(models.ControlState.key == CURSOR_KEY)
                                .with_for_update(skip_locked=True).execution_options(populate_existing=True))
                if row is not None:
                    row.value, row.updated_by = {"after_id": after_id}, ACTOR
            db.flush()
    except Exception:   # never let the advisory cursor fail a tick whose experiment work is already done
        log.warning("experiment scheduler: cursor not saved", exc_info=True)


def _candidates(db: Session, now: datetime, after_id: int) -> tuple[list[int], bool]:
    """Up to SCAN_CAP candidate ids: those after the cursor, then wrapping to the start. True if more remain."""
    due = or_(and_(models.Experiment.status == S.RUNNING, models.Experiment.measure_after <= now),
              models.Experiment.status == S.AWAITING_MEASUREMENT)
    q = select(models.Experiment.id).where(due).order_by(models.Experiment.id)
    ids = db.scalars(q.where(models.Experiment.id > after_id).limit(SCAN_CAP + 1)).all()
    if len(ids) <= SCAN_CAP:
        ids += db.scalars(q.where(models.Experiment.id <= after_id).limit(SCAN_CAP + 1 - len(ids))).all()
    return ids[:SCAN_CAP], len(ids) > SCAN_CAP


def _evaluated(exp: models.Experiment) -> dict:
    ev = exp.evidence or {}
    return {"experiment": exp.key, "outcome": exp.outcome, "baseline_observation_id": ev["baseline"]["observation_id"],
            "result_observation_id": ev["result"]["observation_id"], "source": ev["result"]["source"],
            "unit": ev["result"]["unit"], "window_hours": ev["window_hours"], "delta": ev["delta"],
            "delta_pct": ev["delta_pct"], "evaluated_at": exp.evaluated_at.isoformat() if exp.evaluated_at else None,
            "claim": ev["claim"], "causality": ev["causality"], "statistically_proven": ev["statistically_proven"]}


def _waiting(exp: models.Experiment, now: datetime) -> dict:
    deadline = experiments._aware(exp.measure_after) + metrics_store.stale_after(exp.metric_selector["source"])
    return {"measure_after": experiments._aware(exp.measure_after).isoformat(), "deadline": deadline.isoformat(),
            "overdue": now > deadline}   # overdue is information, not failure: the founder or operator decides


def _step(db: Session, exp: models.Experiment, now: datetime, commit: bool, step, what: str) -> Outcome | None:
    """Run one core transition in its own SAVEPOINT. Returns an Outcome for a refusal or error, None on success."""
    try:
        with db.begin_nested():
            step()
        return None
    except experiments.InsufficientEvidence as e:
        detail = {"permanent": not e.temporary, "message": str(e)[:200]}
        if e.temporary:
            return Outcome(exp.key, WAITING_FOR_DATA, e.reason, detail | _waiting(exp, now))
        return Outcome(exp.key, INSUFFICIENT_EVIDENCE, e.reason, detail)
    except experiments.ExperimentError as e:
        return Outcome(exp.key, BLOCKED, what, {"message": str(e)[:200]})
    except Exception as e:   # one bad experiment must not stop the others; the type only, never a value
        log.exception("experiment scheduler: %s failed for %s", what, exp.key)
        return Outcome(exp.key, ERROR, what, {"error": type(e).__name__})


def _process(db: Session, exp_id: int, now: datetime, result: TickResult, commit: bool) -> None:
    exp = db.scalar(select(models.Experiment).where(models.Experiment.id == exp_id)
                    .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if exp is None:   # another scheduler holds it (or it is gone): that one reports it
        result.outcomes.append(Outcome(f"id:{exp_id}", SKIPPED, "LOCKED_BY_ANOTHER_WRITER"))
        return
    out = []
    if exp.status == S.RUNNING:
        if exp.measure_after is None:
            out.append(Outcome(exp.key, BLOCKED, "NO_MEASURE_AFTER", {}))
        elif now >= experiments._aware(exp.measure_after):
            bad = _step(db, exp, now, commit, lambda: experiments.close_window(db, exp, ACTOR, now), "close_window")
            if bad:
                out.append(bad)
            else:
                out.append(Outcome(exp.key, WINDOW_CLOSED, None, {"measure_after": experiments._aware(exp.measure_after).isoformat()}))
                result.transitions += 1
    if exp.status == S.AWAITING_MEASUREMENT:
        bad = _step(db, exp, now, commit, lambda: experiments.evaluate(db, exp, now), "evaluate")
        if bad:
            out.append(bad)
        else:
            out.append(Outcome(exp.key, EVALUATED, exp.outcome, _evaluated(exp)))
            result.transitions += 1
    result.outcomes.extend(out)
    result.advanced += any(o.category in (WINDOW_CLOSED, EVALUATED) for o in out)
    if commit:
        db.commit()   # also releases the row lock; a failed step already rolled back to its savepoint


def tick(db: Session, now: datetime | None = None, limit: int = DEFAULT_LIMIT, *, commit: bool = False,
         dry_run: bool = False) -> TickResult:
    """Advance eligible experiments (at most `limit` of them change state), rotating fairly through candidates.
    Raises FutureClock, before any database access, when a state-changing tick is given a future `now`."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    now = _utc(now)
    if not dry_run and now > _real_now() + CLOCK_SKEW:
        raise FutureClock(f"now {now.isoformat()} is ahead of the real clock by more than {CLOCK_SKEW}")
    result = TickResult(now=now.isoformat(), limit=limit, dry_run=dry_run)
    ids, result.truncated = _candidates(db, now, _cursor(db))
    last = None
    outer = db.begin_nested() if dry_run else None
    try:
        for exp_id in ids:
            if result.advanced >= limit:
                result.truncated = True
                break
            _process(db, exp_id, now, result, commit and not dry_run)
            last = exp_id
    finally:
        if outer is not None:
            outer.rollback()   # a dry run reports what would happen and keeps none of it (cursor included)
    if last is not None and not dry_run:
        _save_cursor(db, last, create=commit)
        if commit:
            db.commit()
    return result


def run_if_enabled(db: Session, now: datetime | None = None, limit: int = DEFAULT_LIMIT) -> TickResult | None:
    """The call a worker or watchdog tick makes: nothing at all unless both switches are set (`apply_enabled`)."""
    return tick(db, now, limit, commit=True) if apply_enabled() else None


def main(argv: list[str] | None = None, session_factory=None) -> int:
    """One-shot, safe by default: a dry run that changes nothing. `--apply` additionally needs both
    EXPERIMENT_SCHEDULER_ENABLED=true and EXPERIMENT_SCHEDULER_APPLY=yes, checked before a session is opened."""
    ap = argparse.ArgumentParser(description="Advance elapsed experiment windows (dry run unless --apply).")
    ap.add_argument("--apply", action="store_true", help="persist the changes (also needs EXPERIMENT_SCHEDULER_APPLY=yes)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--now", help="ISO-8601 time to evaluate at (default: now, UTC)")
    args = ap.parse_args(argv)
    if args.apply and not apply_enabled():
        print("experiment-scheduler: --apply needs EXPERIMENT_SCHEDULER_ENABLED=true and "
              "EXPERIMENT_SCHEDULER_APPLY=yes; nothing changed", file=sys.stderr)
        return 2
    now = datetime.fromisoformat(args.now) if args.now else None
    if args.apply and now is not None and _utc(now) > _real_now() + CLOCK_SKEW:
        print(f"experiment-scheduler: --now is ahead of the real clock by more than {CLOCK_SKEW}; nothing changed",
              file=sys.stderr)
        return 2
    if session_factory is None:
        from app.db import SessionLocal as session_factory
    db = session_factory()
    try:
        out = tick(db, now, args.limit, commit=args.apply, dry_run=not args.apply)
        if args.apply:
            db.commit()
        print(json.dumps(out.to_dict(), indent=2))
        return 1 if out.counts.get(ERROR) else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
