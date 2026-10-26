# Repository Guardian — C0 audit and C1 slice

Audit date: 2026-10-05. Initial source main: `2dd696fc3cb7a32adf472124faf32a27a0c0ee71`.
Final base refreshed to `6da334880cbc429c2f832212f5dc9c4e5e43fb7e` after #51 merged.
This is source evidence, not a deployment claim.

## Current reality

Stage A exists: watchdog, incidents/events, backups, health, metric observations,
Founder Desk and deterministic Needs You. Stage B exists: PostgreSQL polling,
atomic claiming, subscription providers/zero-cost guard, cooldown, bounded retries,
crash recovery, scoped context and deterministic supervisor. Preserve these.

Relevant merges include #44 external-state/execution separation, #45 database
open-work uniqueness, #46 Needs You, #47 UNKNOWN handling, #49 metric provenance
and portfolio, and #50 Founder Desk command center. The observed PR CI runs passed.
#51 subsequently added Desk design/project/execution views and worker heartbeat
autonomy flags. It is included in the final base; no duplicate UI is added.
CI currently runs on pull requests only: that does **not** prove a CI result for
the merge commit on main.

Live read-only audit found schema `c304f9d92b7a`; the #45 unique index and #49
metric schema were not live. Duplicate open task groups were zero at preflight.
Worker code-write and automatic-external-release flags were OFF. Images had no
commit revision labels; a source SHA cannot be inferred from container health.
API image ID: `sha256:4aa143b501b6c405d687975c274933f45c1c3c5d6591e88ae577618dad6a7940`.
Worker image ID: `sha256:3637c969f1b0da2f6a8e45afcdc2397275fbeb5bf12c05d3827d5c819bf4eb57`.
These are observations from this audit, not permanent facts.

TASK-0003 was recorded DONE, resolving the documented public-control-plane
prerequisite. Founder-approved Stage C design, coder/egress sandbox, repository
lease/fencing, golden security tests and independently fingerprint-bound review
are still prerequisites. Stage C remains OFF. No coder access is granted here.

The existing Herdr worktree/check/fingerprint machinery is the execution seam.
Its LOW quick path may auto-approve without an independent reviewer; that path
must **not** be used as Stage C independent approval. No third task engine is added.

GitHub ruleset/protection reads returned 403 with a private-repository plan
restriction. Protection is unverified; do not assume it or purchase an upgrade.
Proposed future settings require founder review: PR-only main changes, required
existing CI, no force push/deletion, and an appropriate up-to-date requirement.
Actions allowance/spending-limit evidence remains a separate founder check.

## First PR: current-main CI findings, read-only repository access

Problem: Company OS has no durable repository observation checkpoint or bounded
evidence-to-task reconciliation. C1 observes only exact default-branch SHA and
configured GitHub workflow results; it does not claim to detect every architectural
regression or inspect raw logs. Integrity/dependency/business scans are later slices.

Files: `app/repo_guardian.py`, `app/repo_guardian_github.py`,
`agents/repository_guardian.yaml`, Guardian tests and this runbook.
The existing PostgreSQL CI test file gains concurrency coverage; no second workflow.
No migrations, new database tables, agents, runtime service or cron are introduced.

Only ACTIVE projects with canonical repository metadata in the explicit Git
allowlist qualify. DEGRADED/MAINTENANCE/unknown projects and descriptive repository
prose do not silently gain recurring work. Initial allowlist is sez-company only.
YouTube is excluded until its project metadata/state and permission are settled.

Each repository has a unique `ControlState` checkpoint, serialized with a database
row lock. It records last seen/audited/successful-CI SHA, observation time, handled
failure fingerprints and open work references. An incomplete/unknown CI observation
never becomes a successful audit. Baseline observation is not a historical audit.

A failed/timed-out/action-required current-main workflow creates at most two open
Guardian tasks per repository. Stable finding fingerprints use existing database
open-work uniqueness. Concurrent controllers commit one task/event pair. Closed
findings do not reopen on the same SHA. Different repositories do not share a lock.
Repeated findings on a new SHA refresh the existing task's evidence with an audit
row, preserving the original finding. Attended Context Builder handoff carries
escaped, explicitly untrusted evidence through its existing tool-state field.
The live database must contain `uq_tasks_open_dedupe` before apply is permitted.

Tasks are P1/MEDIUM, approval-required and explicitly non-autonomous/non-write.
CI failure proves a problem to **classify**, not a product root cause. Scope,
evidence, expected effect and success criteria are persisted with audit/event rows.
External file/check strings reach model context only as external inputs, never
as trusted instructions. No model runs as part of reconciliation.

## Security and failure modes

The observer is a control-plane process with DB access and existing GitHub **read**
access. Its adapter issues GET requests only. No credential value is copied into
task evidence, subprocess arguments, logs or a workspace. GitHub write capability
is neither required nor used. There is no coder process or credential handoff.

It never clones or executes observed repository code/tests, invokes providers,
creates a worktree, pushes, opens a PR, merges or deploys. CLI reads only bounded
workflow metadata and up to 100 changed paths, without raw logs or patches.
Reads time out in 20 seconds each; no internal retry loop exists. Network failures
roll back and do not advance checkpoints. New spend is $0.

Global pause and project/business-unit eligibility are rechecked after checkpoint
locking, including changed repository identity and business-unit relinking.
Snapshot repository/SHA/time/path validation and checkpoint-base matching
reject wrong scope, stale evidence and out-of-order updates. All mutations commit
in one transaction. Full uniqueness migrations require their own approved rollout.

## Use and rollout boundary

The Git policy defaults `enabled: false`. Preview is read-only even while disabled:

```sh
python -m app.repo_guardian_github
```

After independent review, migration preflight/backup and founder-approved deployment
of an exact source/image, enable the policy in a reviewed change and run one pass:

```sh
python -m app.repo_guardian_github --apply
```

This command is a proposal, **not** approval to run against production. An apply
with disabled policy returns `disabled` before DB/network access. Start with one
manual reconciliation; scheduling every few hours is a later activation decision.
No weekly LLM scan, CI-on-main activation, or new credential is assumed here.

Rollback: stop reconciliation and restore `enabled: false`; global pause stops
task admission too. Existing checkpoint/tasks/audits stay intact. Do not delete
production rows to roll back. The existing worker remains unchanged.

## Acceptance and verification

- Real GitHub main is observed, without mutation; absent main CI is UNKNOWN.
- Fixture failed current-main CI produces an attended task and evidence/audit/event.
- Same finding, concurrent reporters and unchanged reconciliation create no duplicate.
- Different locked repositories continue independently.
- Disabled policy, global pause, inactive project/unit, bad scope or stale evidence
  cannot admit work; no-data cannot become healthy/audited.
- Existing compile, single migration head, Python, PostgreSQL and proxy checks pass.

Live activation still needs exact deployed source/image provenance, index
verification, an approved single-pass pilot and observed production rows. Local
fixtures/read-only GitHub evidence are not that proof.

Verified on the final #51 base: 336 Python tests, 11 real PostgreSQL 18 state/
concurrency tests, 3 proxy tests, compile and one Alembic head passed. PostgreSQL
used disposable `company_external_release_test`, network-isolated socket-only
containers on the existing host, and existing immutable image IDs; fixture
resources were removed. No production database was used. Real GitHub GET reads
returned `6da3348…` with no matching main CI, correctly UNKNOWN. Disabled `--apply`
returned `disabled` without admitting work. Independent spec/standards review
found stale evidence and a post-lock scope race; regression tests reproduce both,
the fixes were re-reviewed, and no remaining blocker was reported.

This branch has not been deployed or activated. Its own remote CI has not run
until a PR is opened; the passing historical PR CI belongs to the base commits.

## Next slices, in dependency order

1. Approved idempotency/provenance deployment; one C1 observer pilot, no coder.
2. C2 coder/egress sandbox and golden security tests; OFF until independent proof.
3. C3 reuse Herdr worktrees plus PostgreSQL repository lease/fencing/recovery.
4. C4/C5 bounded changes, CI-aligned checks and independent review of exact diff hash.
5. C6 control-plane auto-PR only, then bounded C7 CI feedback and C8 outcome verification.

No autonomous merge/deploy, governance rewriting or business-experiment loop is
included in this PR. Successful observation is not successful self-improvement.
