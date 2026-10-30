# Permanent facts

This file contains only stable, founder-confirmed facts. Binding rules live in the
[constitution](../CLAUDE.md) and [Git workflow](GIT_WORKFLOW.md). Dated or mutable
execution state belongs in [`operations/ACTIVE_WORK.md`](operations/ACTIVE_WORK.md);
founder decisions and their status belong in [`operations/DECISIONS.md`](operations/DECISIONS.md).
Never store secrets, credentials, personal data, pending CI, mutable SHAs, or unverified
production claims here.

## FACT-01 · Repository responsibilities

- **Fact:** `Seerxo/seerxo-os` is the primary repository for normal Seerxo OS development
  and CI. `Seerxo/os` retains release and migration responsibilities until the signed
  production cutover is verified.
- **Consequence for agents:** do not move routine engineering back to the previous private
  self-hosted CI path. Source migration does not establish production migration. See
  [DEC-001](operations/DECISIONS.md#dec-001-public-repository-is-the-development-path).
