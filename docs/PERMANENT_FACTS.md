# Permanent facts

This file contains only stable, founder-confirmed facts. Binding rules live in the
[constitution](../CLAUDE.md) and [Git workflow](GIT_WORKFLOW.md). Dated or mutable
execution state belongs in [`operations/ACTIVE_WORK.md`](operations/ACTIVE_WORK.md);
founder decisions and their status belong in [`operations/DECISIONS.md`](operations/DECISIONS.md).
Never store secrets, credentials, personal data, pending CI, mutable SHAs, or unverified
production claims here.

## FACT-01 · Repository responsibilities

- **Fact:** `Seerxo/seerxo-os` is the only active repository: development, CI and the
  production release chain (founder-signed ledger rebase, policy v4 naming
  `seerxo/seerxo-os`, 2026-10-31). `Seerxo/os` is archived and read-only; it has no release,
  migration or CI role.
- **Consequence for agents:** do not move routine engineering back to the previous private
  self-hosted CI path. Source migration does not establish production migration. See
  [DEC-001](operations/DECISIONS.md#dec-001-public-repository-is-the-development-path).
