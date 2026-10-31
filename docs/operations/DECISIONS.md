# Founder decisions

Durable decisions confirmed by the founder and recorded on 2026-10-30. The recorded date
does not claim the original decision date. Do not silently reverse an active decision;
record supersession only after founder confirmation. Mutable execution state belongs in
[ACTIVE_WORK.md](ACTIVE_WORK.md).

## DEC-001 · Public repository is the development path

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** Normal Seerxo OS engineering and CI; private release/migration responsibilities
- **Decision:** `Seerxo/seerxo-os` is the primary path for normal engineering and CI. Do not
  return routine development CI to the previous private self-hosted setup. Since the
  signed production cutover (ledger rebase, policy v4, 2026-10-31) `Seerxo/os` is archived and
  holds no release or migration role.
- **Rationale:** Private self-hosted CI caused serious performance and queueing problems;
  public development CI was the deliberate solution.
- **Evidence:** [Public PR #29](https://github.com/Seerxo/seerxo-os/pull/29) merged
  2026-10-30. Its merge does not prove production cutover; see [ACTIVE_WORK.md](ACTIVE_WORK.md).
- **Status:** ACTIVE

## DEC-002 · Batch related small changes into focused PRs

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** Pull request planning in both repositories
- **Decision:** Finish related work and local checks before opening one focused,
  independently reviewable PR. Related docs, component fixes, test corrections and
  supporting refactors are batched. A small fix alone does not justify a PR.
- **Rationale:** Avoid unnecessary PRs while preserving independent review and required CI.
- **Evidence:** Founder-confirmed operating decision; detailed policy in
  [GIT_WORKFLOW.md](../GIT_WORKFLOW.md).
- **Status:** ACTIVE

## DEC-003 · Avoid unnecessary branch updates

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** Branch maintenance and exact-head CI evidence
- **Decision:** Do not invoke GitHub **Update branch** only because a branch is behind main.
  If mergeable and rules permit, leave it unchanged. If an update is required, use the
  authorized workflow and rerun checks for the changed head.
- **Rationale:** Unnecessary updates change the SHA and invalidate exact-head CI evidence.
- **Evidence:** Founder-confirmed operating decision; see [GIT_WORKFLOW.md](../GIT_WORKFLOW.md).
- **Status:** ACTIVE

## DEC-004 · Prioritize revenue and demonstrated need

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** Seerxo OS engineering priorities and spending
- **Decision:** Prioritize customer problems, revenue blockers, production reliability,
  customer acquisition, product improvement, then necessary automation. Do not add
  infrastructure or major redesign without demonstrated need. No unauthorized spending.
- **Rationale:** Seerxo OS supports a revenue-generating business.
- **Evidence:** Founder-confirmed operating decision.
- **Status:** ACTIVE

## DEC-005 · V1 feature freeze

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** Work before V1 production activation and the real unattended pilot
- **Decision:** No unrelated features, speculative architecture, unnecessary framework
  migration, broad refactors, repetitive general audits without new evidence, or
  unnecessary PRs. Only confirmed activation blockers and essential security corrections
  interrupt the release path.
- **Rationale:** Keep work focused on proving the approved release path.
- **Evidence:** Founder-confirmed operating decision. Current acceptance remains recorded
  in [ACTIVE_WORK.md](ACTIVE_WORK.md), not inferred from this decision.
- **Status:** ACTIVE

## DEC-006 · Report evidence state explicitly

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** CI, review, release, production, and pilot claims
- **Decision:** Use `PASS`, `FAIL`, `BLOCKED`, `UNKNOWN`, or `NOT RUN`. Historical CI,
  merged PRs, prepared pilot kits, and written release plans do not prove current source,
  production, unattended execution, or signed release state. If live host evidence is
  unavailable, report `UNKNOWN`.
- **Rationale:** Keep claims bounded by current evidence.
- **Evidence:** Founder-confirmed operating decision; current checkpoint in
  [ACTIVE_WORK.md](ACTIVE_WORK.md).
- **Status:** ACTIVE

## DEC-007 · Sensitive actions remain founder-controlled

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** Signing, review, repository policy, merge, production, data, spending, and Stage C
- **Decision:** Agents do not forge or bypass signatures, self-approve, bypass independent
  review, change signing policy, disable branch protection, merge without required
  authorization, deploy to production, run destructive database operations, spend or raise
  budgets, or expand Stage C privileges. A general autonomy request grants no blanket
  authority for these actions.
- **Rationale:** Founder authority and independent safety boundaries remain explicit.
- **Evidence:** Founder-confirmed operating decision; binding rules remain in
  [CLAUDE.md](../../CLAUDE.md) and applicable security runbooks.
- **Status:** ACTIVE

## DEC-008 · Complete authorized work and persist evidence

- **Date:** 2026-10-30 (recorded; original date not supplied)
- **Decision maker:** Founder
- **Scope:** Authorized task execution and session handoff
- **Decision:** Inspect, implement, test, review, complete, and record evidence. Do not
  repeat completed work or produce endless plans. Do not spawn additional agents unless
  necessary. When blocked, record the exact blocker and continue independent authorized work.
- **Rationale:** Persistent evidence and completed work prevent repeated mistakes.
- **Evidence:** Founder-confirmed operating decision; checkpoint procedure in
  [START_HERE.md](../../START_HERE.md).
- **Status:** ACTIVE
