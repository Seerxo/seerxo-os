"""Execution evidence for the business reconciler: when the watchdog actually invoked it, not when it
last created a task (a healthy reconciler often creates none).

One ControlState row, overwritten per invocation (no history, no audit rows). It records observed facts
only. `reconcile()` swallows per-goal errors, so a completed invocation is not proof that every goal was
processed. `reconcile()` returns structured per-goal evidence (`business_reconciler.Reconciled`: goals examined and
the ids that failed). Any failure makes `per_goal_outcome` "FAILED"; "OK" means every ACTIVE goal was examined
without an error, nothing more: it never says a goal is on track. A reconcile callable that returns no such
evidence (or an invocation that raised) falls back to counting the ERROR records it logs on `GOAL_LOGGER`, which
can only under-report, so it reads "FAILED" or "unknown", never "OK".

Isolation: every heartbeat write uses its own short session on the caller's engine, so it can neither commit
the caller's pending ORM changes nor roll them back when the write fails. Overlap: each attempt has an
`attempt_id`; writes lock the row, a start never replaces a newer attempt, and a finish only replaces the
row while the attempt still owns it (an older finish may still advance `last_completed_at`, monotonically).
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models

log = logging.getLogger("company.reconciler_heartbeat")

KEY = "business-reconciler-execution"
SOURCE = "watchdog"
CADENCE = timedelta(minutes=10)  # scripts/watchdog.sh tick
FRESH_FOR = CADENCE * 2          # one missed tick is tolerated; two is stale
MAX_KEYS = 20
GOAL_LOGGER = "company.business_reconciler"  # interim: where reconcile() logs a goal it skipped after an error


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else None


def _locked(s: Session) -> models.ControlState | None:
    return s.scalar(select(models.ControlState).where(models.ControlState.key == KEY)
                    .with_for_update().execution_options(populate_existing=True))


def _update(db: Session, change) -> None:
    """Best effort, in a private session: `change(current_value) -> new_value | None` runs under the row
    lock. Losing a heartbeat must never change what the reconciler or watchdog does, or touch its session."""
    try:
        with Session(bind=db.get_bind()) as s:
            for _ in range(2):  # the second pass sees a row another writer created first
                row = _locked(s)
                current = row.value if row is not None and isinstance(row.value, dict) else {}
                value = change(current)
                if value is None:
                    return
                try:
                    if row is None:
                        s.add(models.ControlState(key=KEY, value=value, updated_by=SOURCE))
                    else:
                        row.value, row.updated_by = value, SOURCE
                    s.commit()
                    return
                except IntegrityError:
                    s.rollback()
    except Exception:
        log.exception("could not record reconciler execution evidence")


def _later(a, b):
    """The later of two ISO instants; an unparseable one never wins."""
    pa, pb = _parse(a), _parse(b)
    return a if pa and (not pb or pa >= pb) else b if pb else None


class _GoalErrors(logging.Handler):
    """Counts the ERROR records `reconcile()` logs for goals it swallowed, on this thread only."""
    def __init__(self):
        super().__init__(logging.ERROR)
        self.thread, self.count = threading.get_ident(), 0

    def emit(self, record):
        if record.thread == self.thread:
            self.count += 1


def _outcome(errors: int) -> dict:
    """Fallback from counted log records: never "OK", because a missing log line is not evidence of success."""
    return {"goal_errors": errors, "per_goal_outcome": "FAILED" if errors else "unknown", "goals_total": None}


def _structured(opened, logged: int) -> dict:
    """Per-goal outcome from the evidence reconcile() returned; the logged count if it returned none."""
    total, failed = getattr(opened, "goals_total", None), getattr(opened, "failed_goal_ids", None)
    if not (_count(total) and isinstance(failed, list) and len(failed) <= total):
        return _outcome(logged)
    errors = max(len(failed), logged)   # a logged failure the result missed still counts
    return {"goal_errors": errors, "per_goal_outcome": "FAILED" if errors else "OK", "goals_total": total,
            "failed_goal_ids": [g for g in failed if _count(g)][:MAX_KEYS]}


def run(db: Session, reconcile) -> list[str]:
    """Invoke `reconcile(db)` exactly once, recording start and finish. Its result and exceptions pass through."""
    attempt, started = uuid.uuid4().hex, _now()
    t0 = time.monotonic()
    goal_log, errors = logging.getLogger(GOAL_LOGGER), _GoalErrors()

    def begin(cur):
        prior = _parse(cur.get("last_attempt_at"))
        if prior and prior > started:
            return None  # a newer attempt already owns the row
        return {"source": SOURCE, "expected_cadence_s": int(CADENCE.total_seconds()), "attempt_id": attempt,
                "last_completed_at": cur.get("last_completed_at"), "started_at": started.isoformat(),
                "last_attempt_at": started.isoformat(), "completed_at": None, "duration_ms": None,
                "created_task_count": None, "created_task_keys": [], "error": None,
                "goal_errors": None, "per_goal_outcome": "unknown", "outcome": "STARTED"}

    def finish(patch, completed_at=None):
        def change(cur):
            done = _later(cur.get("last_completed_at"), completed_at) if completed_at else None
            if cur.get("attempt_id") == attempt:
                return {**cur, **patch, "last_completed_at": done or cur.get("last_completed_at")}
            if completed_at and done != cur.get("last_completed_at"):
                return {**cur, "last_completed_at": done}  # superseded: only the monotonic completion time
            return None
        return change

    _update(db, begin)
    goal_log.addHandler(errors)
    try:
        opened = reconcile(db)
    except Exception as exc:
        _update(db, finish({"outcome": "RAISED", "duration_ms": int((time.monotonic() - t0) * 1000),
                            "error": type(exc).__name__, **_outcome(errors.count)}))
        raise
    finally:
        goal_log.removeHandler(errors)
    done = _now().isoformat()
    keys = [str(k) for k in opened]
    _update(db, finish({"outcome": "COMPLETED", "completed_at": done, "duration_ms": int((time.monotonic() - t0) * 1000),
                        "created_task_count": len(keys), "created_task_keys": keys[:MAX_KEYS],
                        **_structured(opened, errors.count)}, done))
    return opened


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def view(db: Session, now: datetime | None = None) -> dict:
    """Read-only Desk projection. Freshness is computed from the timestamps, never stored."""
    now = now or _now()
    row = db.scalar(select(models.ControlState).where(models.ControlState.key == KEY)
                    .execution_options(populate_existing=True))  # never a stale cached copy
    out = {"last_attempt_at": None, "last_completed_at": None, "outcome": "NO_EVIDENCE", "duration_ms": None,
           "tasks_created": None, "evidence_source": SOURCE, "freshness": "UNAVAILABLE",
           "expected_cadence_s": int(CADENCE.total_seconds()), "per_goal_outcome": "unknown", "goal_errors": None,
           "goals_total": None, "error": None}
    if row is None:
        return out
    v = row.value if isinstance(row.value, dict) else {}
    attempt = _parse(v.get("last_attempt_at"))
    if attempt is None or attempt > now:
        return {**out, "outcome": "MALFORMED"}  # missing, unparseable, naive or ahead of the clock: untrusted
    completed = _parse(v.get("last_completed_at"))
    if completed is not None and completed > now:
        completed = None  # a completion in the future never happened yet
    outcome = v.get("outcome")
    if outcome not in ("STARTED", "COMPLETED", "RAISED") or (outcome == "COMPLETED" and completed is None):
        return {**out, "outcome": "MALFORMED"}  # a COMPLETED claim needs a valid completion time
    if outcome == "STARTED" and now - attempt > CADENCE:
        outcome = "UNFINISHED"  # started, never finished: crash or kill, not a success
    duration, count, goal_errors = v.get("duration_ms"), v.get("created_task_count"), v.get("goal_errors")
    goal_errors = goal_errors if _count(goal_errors) else None
    goals_total = v.get("goals_total") if _count(v.get("goals_total")) else None
    # "OK" only from stored structured evidence of a completed invocation with zero errors; anything less is unknown.
    per_goal = ("FAILED" if goal_errors else
                "OK" if outcome == "COMPLETED" and v.get("per_goal_outcome") == "OK" and goal_errors == 0
                and goals_total is not None else "unknown")
    return {**out, "last_attempt_at": attempt.isoformat(),
            "last_completed_at": completed.isoformat() if completed else None, "outcome": outcome,
            "duration_ms": duration if _count(duration) else None,
            "tasks_created": count if _count(count) else None,
            "goal_errors": goal_errors, "per_goal_outcome": per_goal, "goals_total": goals_total,
            "freshness": "STALE" if now - attempt > FRESH_FOR else "VERIFIED",
            "error": v.get("error") if isinstance(v.get("error"), str) else None}
