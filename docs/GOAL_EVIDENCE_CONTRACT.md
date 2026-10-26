# Goal evidence contract

Implemented in `app/goal_evidence.py` over `goals.assess()` (`app/goals.py`). Read-only: it
queries `metric_observations` and the goal row, writes nothing, calls no model, and never
turns a missing reading into zero or an ambiguous one into success.

## Interface

```python
from app import goal_evidence
ev = goal_evidence.for_goal(db, goal, now=None)   # frozen dataclass GoalEvidence
ev.to_dict()                                      # JSON-serializable dict, adds "explanation"
```

One selection algorithm: `goals.resolve(db, selector, now)` returns `(row, reason_code)`.
`goals.latest`, `goals.progress` (unchanged shape) and `GoalEvidence` all derive from it, so they
cannot disagree. Same database state and same `now` give an identical payload.

## Fields

| Field | Meaning |
|---|---|
| `goal_id`, `metric_selector` | The goal and its persisted selector, as stored (may be malformed). |
| `measurement_status` | `VERIFIED` (fresh), `STALE` (older than the source's freshness window), `UNAVAILABLE`. |
| `reason_code` | Why the measurement is or is not usable (below). |
| `source`, `observation_id`, `observed_at`, `measurement_unit`, `latest_value` | Provenance of the newest observation; all `null` when `UNAVAILABLE`. A `STALE` reading keeps them so the age is visible. |
| `baseline`, `target` | Parsed numbers, or `null` when absent, non-numeric or non-finite. |
| `progress` | `(value - baseline) / (target - baseline)` rounded to 4 places, else `null`. |
| `progress_reason_code` | `PROGRESS_COMPUTED`, `BASELINE_INVALID`, `TARGET_INVALID`, `TARGET_EQUALS_BASELINE`, or `null` when there is no usable value. |
| `evidence_timestamp` | The evaluation time (`now`), ISO-8601 UTC. |

## Reason codes (`reason_code`)

`MEASUREMENT_VERIFIED`, `MEASUREMENT_MISSING` (no row for the series), `MEASUREMENT_STALE`,
`SOURCE_AMBIGUOUS` (unpinned selector, several sources), `UNIT_CONFLICT` (one source, several
units), `SELECTOR_INVALID`, `FUTURE_OBSERVATION` (every row dated after `now`),
`INVALID_NUMERIC_EVIDENCE` (newest value is not a finite number). Codes are stable; unknown
codes raise `ValueError` at construction.

## Examples

Verified:

```json
{"goal_id": 1, "metric_selector": {"subject": "youtube:channel", "metric": "views", "window": "7d"},
 "measurement_status": "VERIFIED", "reason_code": "MEASUREMENT_VERIFIED", "source": "youtube-data-api",
 "observation_id": 1, "observed_at": "2026-10-08T11:00:00+00:00", "measurement_unit": null,
 "latest_value": 12500.0, "baseline": 10000.0, "target": 20000.0, "progress": 0.25,
 "progress_reason_code": "PROGRESS_COMPUTED", "evidence_timestamp": "2026-10-08T12:00:00+00:00",
 "explanation": "a current observation from a single source and unit"}
```

Ambiguous (two sources, unpinned selector):

```json
{"goal_id": 1, "metric_selector": {"subject": "youtube:channel", "metric": "views", "window": "7d"},
 "measurement_status": "UNAVAILABLE", "reason_code": "SOURCE_AMBIGUOUS", "source": null,
 "observation_id": null, "observed_at": null, "measurement_unit": null, "latest_value": null,
 "baseline": 10000.0, "target": 20000.0, "progress": null, "progress_reason_code": null,
 "evidence_timestamp": "2026-10-08T12:00:00+00:00",
 "explanation": "several sources report this series and the selector does not pin one"}
```

## Reconciler tasks

`business_reconciler` stores a bounded snapshot at `task.context["evidence"]` for each task it
opens (`GOAL_ACHIEVED`, `GOAL_UNMEASURED`, `GOAL_NO_WORK`, `GOAL_BEHIND`): `rule`, `why`,
`goal_id`, `goal_title` (120 chars), `selector` (four string keys, 200 chars each),
`measurement_status`, `reason_code`, `observation_id`, `observed_at`, `source`/`value`/`unit`
(only when `VERIFIED`), `baseline`, `target`, `progress`, `expected_progress`,
`missing_evidence` (reason codes), `would_change_if` (fixed text), `evidence_timestamp`.
Older tasks have no `evidence` key; consumers must treat it as optional. Dedupe keys, cooldown,
approval, agent assignment and status semantics are unchanged.

## Goal configuration (`GOAL_CONFIG_INVALID`)

`goals.config_issues(goal)` judges the goal itself, whatever the metric reads: `SELECTOR_INVALID`
(also for a selector that is not an object), `BASELINE_INVALID`, `TARGET_INVALID` (missing,
non-numeric, NaN or infinite), `TARGET_EQUALS_BASELINE`. A goal whose selector is absent (`null`) is still
skipped; a present but malformed one, including a falsy `{}`, `[]`, `""`, `0` or `false`, is `SELECTOR_INVALID`. When any issue exists the reconciler opens only `GOAL_CONFIG_INVALID` (founder-attended,
`approval_required`, never autonomous, no agent), checked before `GOAL_UNMEASURED`, and never
`GOAL_ACHIEVED`, `GOAL_NO_WORK` or `GOAL_BEHIND`. It does not edit the goal. Its snapshot adds
`config_issues`, sets `reason_code` to the first issue, keeps the reading's own state in
`measurement_status` and `measurement_reason_code`, and lists both in `missing_evidence`
(config issues first).

## Not implemented

No experiment tables, lifecycle, learning approval, API route or migration. A consumer such as
the Experiment Engine should call `for_goal` and treat any `measurement_status` other than
`VERIFIED` as "no evidence".
