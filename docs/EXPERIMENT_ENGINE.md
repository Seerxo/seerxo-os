# Experiment engine (core)

`app/experiments.py` answers "did this improvement work?" as an observed difference between two existing
`metric_observations` rows of the exact same series. It never claims causality: there is no control group, so the
evidence always says `control_group: null`, `statistically_proven: false`, `causality: NOT_ESTABLISHED`.
This package contains the core and the table only. API routes belong to Agent 3 (`app/main.py`, not edited here).

## Lifecycle

`PROPOSED -> READY -> RUNNING -> AWAITING_MEASUREMENT -> EVALUATED -> ACCEPTED | REJECTED`; `CANCELLED` from any
state before `EVALUATED`. Transitions are the `NEXT` table; anything else raises `ExperimentError`.

| Function | Who | Effect |
|---|---|---|
| `propose(db, *, title, hypothesis, business_unit, metric_selector, expected_direction, measurement_window_hours, actor, min_effect=0, goal_id=None, project_id=None, now=None)` | any actor | New `PROPOSED`, or the already-open identical experiment (same fingerprint). |
| `approve_ready(db, exp, founder, now=None)` | founder only | `READY`; pins a fresh baseline observation of the exact series. |
| `start(db, exp, intervention_ref, actor, now=None)` | any actor | `RUNNING`; the change was made elsewhere, through its own approvals. |
| `close_window(db, exp, actor=SYSTEM, now=None)` | any actor | `AWAITING_MEASUREMENT`, only after the window elapsed. |
| `evaluate(db, exp, now=None)` | system | `EVALUATED` with `outcome` and `evidence`. |
| `decide(db, exp, accept, founder, reason, now=None)` | founder only | `ACCEPTED` / `REJECTED`. |
| `cancel(db, exp, actor, reason)` | any actor | `CANCELLED`. |
| `propose_learning(db, exp, actor)` | any actor | A `CANDIDATE` learning for a decided experiment. |
| `view(exp)` / `goal_link(db, exp)` | read | JSON-safe dict / goal-link evidence. |

Nothing here writes to goals, tasks, deploys or spending. Every transition locks the experiment row
(`SELECT ... FOR UPDATE`), is idempotent when replayed in the same state, and writes one audit row.

## What the engine refuses (insufficient evidence, never a success)

- Baseline missing, stale for its source, in the wrong unit, or not in the experiment's series (checked at READY,
  again at start, and again at evaluation).
- No reading after the window, or the first reading after the window arrived later than the source's freshness
  window (the source went silent, so it no longer represents the window).
- Result in another unit or series.
- Outcome rules: `IMPROVED` / `WORSENED` only when the signed change reaches `min_effect`, else `NO_MEANINGFUL_CHANGE`.

## Goal consistency

An experiment may name a `goal_id`. Rules (`_goal_check`), applied at proposal and revalidated at READY, start and
acceptance:

- The goal exists and is not closed (closed goals are only tolerated at acceptance, so a goal that was achieved
  meanwhile does not strand a finished experiment).
- The goal has a project and that project's business unit is the experiment's; a given `project_id` must be the
  goal's project. A goal without a project has no provable business unit and is rejected.
- The goal selector is valid (`goals.selector_valid`) and measures the same `subject`, `metric` and `window`
  (a missing goal window means the default `""`).
- Goal selector with a `source`: must equal the experiment's source (`EXACT_SOURCE`).
- Goal selector without a `source`: accepted only while `goals.resolve` (the single metric-selection algorithm)
  resolves it to the experiment's source, i.e. one reporter (`SOURCELESS_RESOLVED`). No reading, a second reporter
  or another source rejects it: pin the source on the goal.
- The goal's selector is snapshotted in `goal_selector_snapshot` at proposal. A later edit blocks READY, start and
  acceptance (`goal selector changed`). Rejecting and cancelling stay allowed. Evaluation never refuses for it; it
  records `evidence.goal_link` (`LINKED` with mode, `NO_GOAL`, or the broken reason) so the decision sees it.

## Learning pipeline

`propose_learning` creates `LearningStatus.CANDIDATE` only, worded "observed difference only; causality not
established". The engine has no path to `APPROVED`; the existing review (`app/main.py` learning approval) decides.

## API integration contract (for Agent 3)

- Callers commit; the core never does. On any exception call `db.rollback()`.
- Map `ExperimentError` -> 409 (rule broken, nothing changed), `NotFounder` -> 403. An unknown key is a 404 you
  raise before calling. Errors leave no partial write once rolled back (tested).
- **Founder identity (integration blocker for Agent 5's routes).** The engine accepts a founder-only step
  (`approve_ready`, `decide`) only when the `founder` argument is exactly `founder:access:<lowercase email>`
  (`is_founder`; at most 100 characters, the `decided_by`/`approved_by` column size). That is the id of Agent 5's
  verified founder `Principal` (`security/approval-identity`, `docs/APPROVAL_IDENTITY.md`). Anything else raises
  `NotFounder`: `founder`, `founder (...)`, `founder:access:` with a malformed or upper-case email, `operator:*`,
  `service:*`, `anonymous` and any other string that merely starts with "founder". The earlier `founder (via
  <caller>)` form is gone.
- **The core cannot authenticate.** `is_founder` is a format guard. Authorization must come from a verified
  principal, never from a client-provided string. The route layer (Agent 5) must:
  1. call `approval_identity.require_founder(request)` for `POST /experiments/{key}/approve`, `.../accept` and
     `.../reject`, and pass `principal.id` as `founder`;
  2. pass `approval_identity.principal_of(request).id` as `actor` for propose, start, close-window, cancel and
     learning (an operator or service principal records as itself and can never satisfy a founder-only step);
  3. never read an actor or founder value from the body, a header or a query string, and never build the string
     (`f"founder:access:{...}"`) itself;
  4. keep every `POST /experiments...` route inside `api_auth.ENFORCE_ALWAYS`, and map `NotFounder` -> 403,
     `ExperimentError` -> 409.
  Non-founder actors must be 1-100 characters, without leading or trailing space or control characters, or the
  engine raises `ExperimentError` (`principal.id` already satisfies this).
- Until those routes exist and use the verified principal, no founder-only transition can be reached from HTTP.
  This package adds no route.
- `propose` is idempotent for an identical open proposal (returns the existing one, `201` vs `200` is your call by
  comparing `exp.created_at`/`key` beforehand if you care). Replays of the other steps in the same state return the
  current row; a replay after the state moved on is a 409.
- Suggested routes: `GET /experiments`, `GET /experiments/{key}`, `POST /experiments`,
  `POST /experiments/{key}/{approve|start|close-window|evaluate|accept|reject|cancel|learning}`. Schemas
  `ExperimentCreate` and `ExperimentAction` exist in `app/schemas.py`. `ExperimentCreate` does not carry `now`.
- Example `view(exp)` payload keys: `key, title, hypothesis, status, goal_id, metric_selector, expected_direction,
  min_effect, measurement_window_hours, intervention_ref, measure_after, outcome, evidence, approved_by, decided_by,
  decision_reason, learning_id, result_freshness`.
- `evaluate` and `close_window` are meant for a scheduler or the founder; nothing in this package schedules them.

## Deployment note

The experiments migration is part of the normal Alembic chain. A release applies a schema change only when it
explicitly authorizes the exact target revision (`ALLOW_MIGRATION`), and a migration that has run is not undone
automatically; see the rollback decision below and [security/RELEASE_GATEWAY.md](security/RELEASE_GATEWAY.md).

## Migration `b4f8a2d6c1e9` (down_revision `c7d2e8a4f1b3`)

Additive: one table, one enum type, indexes `key` (unique), `status`, `goal_id`, and the partial unique index
`uq_experiments_open_fingerprint` (one open experiment per proposal). CHECK constraints: direction in
`UP/DOWN`, `min_effect >= 0`, `measurement_window_hours > 0`. Foreign keys are RESTRICT, so an observation, goal or
learning an experiment cites cannot be deleted from under it. It has been applied in production.
Verified locally on PostgreSQL 17.11 and in CI on PostgreSQL 18.6 (`postgres:18-alpine`) for the engine and
scheduler pull requests; that is test evidence, not a statement about the production database version.

### Downgrade destroys experiment rows

`alembic downgrade c7d2e8a4f1b3` drops the `experiments` table and its enum, and with them every experiment,
its evidence and its decisions. The downgrade now refuses while the table has rows, unless
`EXPERIMENTS_DOWNGRADE_CONFIRM_DATA_LOSS=yes` is set. Audit rows and candidate learnings are separate tables and
stay.

### Attended rollback plan (prefer this)

1. Roll back the application image only. The schema is additive; older code ignores the table. Leave the table.
   The table now exists in production; whether it holds rows is unknown, so never downgrade without step 2.
2. If the schema must go: stop the experiment routes, then export first:
   `\copy (select * from experiments) to 'experiments-<date>.csv' csv header` and keep it with the audit export
   (`select * from audit_log where entity_type = 'experiment'`).
3. Founder confirms the export, then run the downgrade with the confirmation variable.
4. Re-upgrading recreates an empty table; the export is the only copy of the experiments.

## Verification

PostgreSQL 17.11 locally: migration up/down/up, constraints, downgrade guard, and concurrency (conflicting decisions,
duplicate decisions, duplicate proposals, concurrent evaluation, approve racing cancel). The concurrency tests
widen the race window and fail when the row lock is removed.
