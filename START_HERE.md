# Start here

Seerxo OS is an agent-driven operating system for coordinating business operations and software delivery. This page is the shared session entry point for Claude Code, Codex, and other contributors.

## Session startup

1. Read the [operating constitution](CLAUDE.md). `AGENTS.md` is its symlink; both providers follow the same rules.
2. Read the [Git and PR policy](docs/GIT_WORKFLOW.md) before changing branches or publishing work.
3. Load only relevant [permanent facts](docs/PERMANENT_FACTS.md) and [founder decisions](docs/operations/DECISIONS.md).
4. Read the [active checkpoint](docs/operations/ACTIVE_WORK.md). Historical reports are not current state.
5. Refresh GitHub branch, PR, review, and exact-head CI state before relying on saved evidence. Check live production only when needed and authorized; without signed host evidence report `UNKNOWN`.
6. Read only the task-specific technical docs needed for the change. Use the [docs index](docs/README.md) to find their canonical homes.

At session end, update the existing checkpoint with verified evidence and one next action. Do not create a new status report when an existing record owns it.

## Project context

Read [README.md](README.md) for the product, architecture, quickstart, and security model. Read [CONTRIBUTING.md](CONTRIBUTING.md) for contribution details. Preparing a pull request does not grant merge or deployment authority.

For release-specific trust boundaries, see the [threat model](docs/security/THREAT_MODEL.md) and [release gateway](docs/security/RELEASE_GATEWAY.md). Report a vulnerability through [SECURITY.md](SECURITY.md), not a public issue.
