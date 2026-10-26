# Experiment scheduler

`app/experiment_scheduler.py` advances experiments whose measurement window is over, without a human. It is a
callable, not a service. The watchdog tick (`app/watchdog.py: run_experiment_scheduler`, every 10 minutes via
`scripts/company-os.cron`) calls `run_if_enabled()` in its own session. Its persistent entry points are **off unless both
`EXPERIMENT_SCHEDULER_ENABLED=true` and `EXPERIMENT_SCHEDULER_APPLY=yes`** (`apply_enabled()`): without both,
`run_if_enabled()` returns `None` before touching the database and the command's `--apply` refuses before opening a
session (see below). `tick()` itself is the low-level API for tests and for the caller that integrates it; it reads
no switch, so calling it directly is a deliberate decision of that caller. It needs no migration, no new table, no
external call and no model call.

## What a tick does

```python
from app import experiment_scheduler
result = experiment_scheduler.tick(db, now=None, limit=50, commit=False, dry_run=False)   # TickResult
result.to_dict()                                                                          # JSON-safe
```

1. `RUNNING` and `measure_after <= now` -> `experiments.close_window()` -> `AWAITING_MEASUREMENT`.
2. `AWAITING_MEASUREMENT` (including one just closed) -> `experiments.evaluate()` -> `EVALUATED`.

Every rule (series, unit, freshness, ordering, the outcome) is the existing core's; the scheduler only chooses
which experiments to hand it. It never calls `propose`, `approve_ready`, `start`, `decide` or `propose_learning`,
never inserts a metric observation, and never touches goals, tasks, approvals, incidents or learnings. Its only
write outside `experiments` and the audit log is one advisory `control_state` row, `experiment-scheduler-cursor`. An
evaluated experiment is **prepared for founder review**: `outcome` and `evidence` are set, `decided_by` stays empty.
Actors recorded: `system:experiment-scheduler` (closing) and `system:experiments` (the core's evaluation). No
founder principal is created or impersonated; the Agent 5 identity work is untouched.

## Result categories (per experiment, in `result.outcomes`)

| Category | Meaning | Final? |
|---|---|---|
| `WINDOW_CLOSED` | window elapsed, now `AWAITING_MEASUREMENT` | state change |
| `EVALUATED` | `reason` = outcome (`IMPROVED` / `WORSENED` / `NO_MEANINGFUL_CHANGE`); detail has `baseline_observation_id`, `result_observation_id`, `source`, `unit`, `window_hours`, `delta`, `delta_pct`, `evaluated_at`, `claim`, `causality`, `statistically_proven: false` | state change |
| `WAITING_FOR_DATA` | no observation of the exact series after the window yet (`NO_POST_WINDOW_OBSERVATION`); detail has `deadline` and `overdue`. **Temporary, never a verdict**: `overdue` is information for the founder or operator, not a failure | no |
| `INSUFFICIENT_EVIDENCE` | permanently unusable: `LATE_FIRST_OBSERVATION`, `UNIT_MISMATCH`, `SERIES_MISMATCH` (detail `permanent: true`) | stays `AWAITING_MEASUREMENT` until a human cancels it |
| `BLOCKED` | the core refused for another reason, or the row is inconsistent (`NO_MEASURE_AFTER`) | no |
| `ERROR` | an unexpected exception; detail is the exception **type only**. Only that experiment is rolled back | retried next tick |
| `SKIPPED` | another scheduler holds the row (`LOCKED_BY_ANOTHER_WRITER`); that one reports it | no |

Missing data is never classified as failure or success. No statistical significance is computed or implied.
`Outcome.fingerprint` (`key:category:reason`) is stable, so a caller that wants to notify can notify on a *change*
of fingerprint. The scheduler itself creates no incident, task or notification, so an unchanged state produces
no new experiment or audit rows: repeated ticks write nothing for waiting, blocked or finished experiments (the
cursor row is updated in place).

## Safety properties (tested)

- **Idempotent:** a repeated tick finds nothing to do; one audit row per transition, ever.
- **Concurrent schedulers:** each row is claimed `FOR UPDATE SKIP LOCKED`; two or three schedulers advance every
  experiment exactly once, the others skip it instead of waiting. The core's own row lock and status checks stay
  the final guard.
- **Isolation:** each experiment runs in its own `SAVEPOINT`. A failure (including a PostgreSQL error such as
  `division by zero`) rolls back only that experiment, even when the caller owns one transaction.
- **Transactions:** `commit=False` (default) leaves commit/rollback to the caller; `commit=True` commits after
  every experiment, so a crash loses at most the one in flight and the next tick resumes (a window closed but
  not yet evaluated resumes at evaluation). `BaseException` (crash, interrupt) is never swallowed.
- **Bounded:** `limit` bounds experiments *advanced* per tick; at most `SCAN_CAP = 1000` candidates (due `RUNNING`
  and all `AWAITING_MEASUREMENT`) are examined per tick, in id order from a rotating cursor that wraps at the end.
  `truncated` says more work remained this tick. Processing order is id order, not oldest window first.
- **Fair, with one scheduler and a persisted cursor:** the cursor moves to the last candidate the tick reached. A
  tick stops early only after `limit` advances, so it passes at least `min(limit, SCAN_CAP)` candidates (or all of
  them, wrapping). Experiments that cannot progress (waiting for data, permanently insufficient, failing) therefore
  cannot hide others: every candidate is reached within `ceil(N / min(limit, SCAN_CAP))` ticks, where `N` counts
  every candidate that existed meanwhile (new experiments get higher ids and join the rotation). Tested with
  `N = 30`, `SCAN_CAP = 10`, `limit = 2`: 15 ticks, not `ceil(N / SCAN_CAP) = 3`. "Reached" includes a candidate
  skipped because another writer held its row; it is examined on a later lap.
- **What is not guaranteed:**
  - *Concurrent schedulers:* each saves the cursor at the end of its own tick and the last save wins, so the
    cursor can move backwards. That only repeats idempotent work. One PostgreSQL test (three schedulers,
    `SCAN_CAP = 5`, `limit = 2`, 24 waiting experiments, 15 ticks each) shows every window closed exactly once and
    every candidate reached, but no tick bound is claimed.
  - *An unpersisted cursor:* the cursor row is created only by a self-committing tick (`commit=True`, the
    command's `--apply`, `run_if_enabled`), which commits immediately after. An INSERT left uncommitted in a
    caller's open transaction would make every other scheduler's INSERT of the key wait for that transaction (tested
    on PostgreSQL). A `commit=False` tick moves an existing cursor inside the caller's transaction (a rollback
    undoes it), but never creates one. A caller that only ever runs `commit=False` ticks, or never commits, gets no
    rotation and the starvation the cursor exists to prevent. While such a transaction holds the row, other
    schedulers skip their cursor save and repeat that work later.
  - *Crashes:* the cursor is saved once, at the end of a tick. A tick that dies before that keeps every experiment
    it committed and the next tick starts from the old cursor (idempotent repeats). A tick that dies at the same
    point every time never moves the cursor.
  - The cursor is advisory: missing or corrupt (wrong type, negative, beyond the last id), it only restarts the
    rotation (an `after_id` above the 32-bit id range included). An existing cursor row is updated with `SKIP
    LOCKED`, so no scheduler waits on it. Two first-ever self-committing ticks can meet on the INSERT: the second
    waits only until the first commits (it does nothing in between), then skips its save.
- **Time:** `now` is injectable; naive values are read as UTC and offsets are normalized. A state-changing tick
  (`dry_run=False`, with or without `commit`) raises `FutureClock` **before any database access** when `now` is
  more than `CLOCK_SKEW` (5 minutes, the same allowance metric ingestion gives `observed_at`) ahead of the real
  UTC clock. **Within that tolerance a window can close early:** a caller passing `now` up to 5 minutes ahead closes
  a window up to 5 minutes before it ends by the real clock (tested), and evaluation then considers readings up to
  that `now`. This is the accepted limit, not a guarantee of "never early". It is bounded on both sides: windows
  are hours long, and ingestion refuses `observed_at` more than the same 5 minutes ahead, so no reading from
  further in the future exists to be used. Evaluation never decides anything: the founder reviews it. The
  production entry points pass no `now` (the real clock), so the tolerance applies only to an explicit `now`. A
  past `now` is allowed: it cannot close a window early and can only find less evidence. A window ending at exactly
  `now` counts as elapsed. Tests pin the real clock by replacing `_real_now` in-process; there is no flag or
  argument that skips the check.
- **`dry_run=True`** reports what would happen and keeps none of it (the cursor included). It may simulate any
  time, including the future, because nothing it does persists; its row locks end with its savepoint.

## One-shot command (local testing)

`python -m app.experiment_scheduler [--limit N] [--now ISO8601]` is a **dry run**: it prints the JSON result and
changes nothing, whatever the switches. `--apply` requires **both** `EXPERIMENT_SCHEDULER_ENABLED=true` and
`EXPERIMENT_SCHEDULER_APPLY=yes`, and refuses a `--now` ahead of the real clock; every refusal happens before a
database session is opened. Exit codes: `0` ok, `1` an `ERROR` outcome, `2` refused (a switch missing or a future
`--now`). It uses `DATABASE_URL`; do not point it at production without approval.

## Wiring (done) and how to turn it on

`app/watchdog.py: run_experiment_scheduler()` runs after the reconciler on every watchdog tick, in its own session,
and reports `result["experiments"]`: `None` while off, `{"counts", "truncated"}` when it ran, `{"error": <type>}` if
it failed (logged; the watchdog tick and its exit code are unaffected). `docker-compose.yml` passes both switches to
`company-api`, defaulting to off. To enable after founder authorization, set in the host `.env`:
`EXPERIMENT_SCHEDULER_ENABLED=true` and `EXPERIMENT_SCHEDULER_APPLY=yes`, then recreate `company-api` through a
normal authorized deploy. To stop it, unset either. Tests: `tests/test_experiment_scheduler.py` (watchdog section).

## Integration point (original notes for Agent 3 / Agent 4)

Nothing else was edited: not `app/main.py`, `api_auth.py`, `approval_identity.py`, `watchdog.py`, `worker.py`,
`stage_c_worker.py`, `docker-compose.yml` or CI. The minimal wiring, in whichever periodic tick owns it:

```python
# inside an existing periodic tick, with its own Session
from app import experiment_scheduler
try:
    result = experiment_scheduler.run_if_enabled(db)      # None unless both switches are set
except Exception:
    db.rollback()
    log.exception("experiment scheduler failed")           # never let it break the host tick
else:
    if result and result.outcomes:
        log.info("experiments: %s", result.counts)         # log, or notify only on a changed fingerprint
```

- `run_if_enabled` uses `commit=True`, so give it a session the host tick does not otherwise hold open. Pass no
  `now` (the real clock); a future `now` raises `FutureClock`.
- **Cadence:** windows are measured in hours and metric freshness is hours to days (`metric_sources`), so a tick
  every 10 to 60 minutes is plenty; faster only adds reads. The cost of a tick with nothing to do is one query.
- **Desk:** `EVALUATED` experiments (status `EVALUATED`, `decided_by` empty) are what the founder must decide;
  `WAITING_FOR_DATA` with `overdue: true` and `INSUFFICIENT_EVIDENCE` are what an operator may need to cancel.
- **Dependencies:** `app/experiments.py` (this repo) only. `InsufficientEvidence` is a small typed subclass of
  `ExperimentError` added to the core so the scheduler never parses message text; existing callers are unaffected.

## Failure behavior and rollback

- Disabled by default; to stop it, unset either switch (or remove the call). Its only state is the advisory
  cursor row, which can be left in place or deleted at any time.
- A crashed or failed tick leaves every experiment in a valid state; rerunning is always safe.
- Transitions it makes are ordinary audited experiment transitions. Closing a window and evaluating cannot be
  undone by the scheduler; an evaluation the founder distrusts is rejected (`decide`) or the experiment is
  cancelled before evaluation. There is no schema change to roll back.

## Verification

SQLite (unit): `tests/test_experiment_scheduler.py`. PostgreSQL (concurrency, savepoints, crash recovery, time
zones): `tests/test_experiment_scheduler_postgres.py`; verified locally on PostgreSQL 17.11, and on PostgreSQL 18
only through the pull request's CI.

## Known limit: out-of-order observations (UNKNOWN, not a confirmed defect)

`experiments.evaluate` uses the first observation of the series, after the window, that exists **when the tick
runs**. If a writer later backfills an earlier-dated reading, the stored evidence is not the true first reading.
Review of the configured sources (2026-10-08): `polar-api` stamps `observed_at` with its own collection time,
in-process, so it is in order. `seerxo-backend` uses the live snapshot's `generated_at`, polled hourly, which is
out of order only if the upstream serves an older cached snapshot (no evidence that it does). The three YouTube
sources are HTTP writers that choose `observed_at`, and ingestion only refuses future times, so backfill is
**allowed by the contract**, but no writer for them exists in `youtube-auto` (`main` at `1e658e1`) or in this
repository today. Before a backfilling writer is connected, decide separately whether evaluation should wait out
the source's freshness window or compare `received_at`; nothing is changed here on speculation.
