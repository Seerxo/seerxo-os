# Start here

Seerxo OS is an agent-driven operating system for coordinating business operations and software
delivery. This page is the shortest path for a new contributor or a coding agent.

1. **Read the project overview:** [README.md](README.md) (what it does, architecture, quickstart, security model).
2. **Read the binding rules:** [CLAUDE.md](CLAUDE.md). `AGENTS.md` is the same file for tools that look for that name.
3. **Read the Git workflow before any branch, commit or merge:** [docs/GIT_WORKFLOW.md](docs/GIT_WORKFLOW.md)
   (trunk-based: `main` is the only permanent branch; every change goes through a pull request).
4. **Understand the trust boundaries:** [docs/security/THREAT_MODEL.md](docs/security/THREAT_MODEL.md) and
   [docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md). An agent that prepares a pull request has
   no permission to merge or deploy it.
5. **Run the tests** as described in the README, then open a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

Reporting a vulnerability: see [SECURITY.md](SECURITY.md). Do not open a public issue for it.
