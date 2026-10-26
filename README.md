# Seerxo OS

**An agent-driven operating system for a small company.** Seerxo OS keeps the company's operational
state in PostgreSQL, decides deterministically when there is work to do, hands that work to AI agents
inside strict boundaries, and records what happened so the next session, human or model, starts from
facts instead of memory.

> The guiding rule: *the company must remember even when the AI forgets.* Models are replaceable
> workers. PostgreSQL holds operational state, Git holds policy, and a conversation is temporary.

Status: **V1, in active development.** See [Project maturity](#project-maturity-and-known-limitations).

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Agent lifecycle](#agent-lifecycle)
- [Autonomy levels and approval boundaries](#autonomy-levels-and-approval-boundaries)
- [Technology stack](#technology-stack)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Running the tests](#running-the-tests)
- [Security and trust boundaries](#security-and-trust-boundaries)
- [Production deployment concepts](#production-deployment-concepts)
- [CI](#ci)
- [Project structure](#project-structure)
- [Development workflow](#development-workflow)
- [Contributing](#contributing)
- [Reporting a vulnerability](#reporting-a-vulnerability)
- [Project maturity and known limitations](#project-maturity-and-known-limitations)
- [License](#license)

---

## What it does

- **Operational memory.** Business units, projects, services, tasks, events, incidents, approvals,
  decisions, learnings, goals, metric observations and experiments are rows in PostgreSQL, with an
  audit trail. Nothing important lives only in a model's context window.
- **Deterministic work detection.** A reconciler evaluates goals against measured metrics and opens a
  task only when the evidence says one is needed. No work means no model call.
- **An unattended worker.** Eligible low-risk tasks are claimed atomically, run through a pluggable
  provider (the reference providers drive the Claude and Codex command-line tools), checkpointed, and
  recovered after crashes without repeating completed model calls.
- **Isolated code changes ("Stage C").** A coding agent edits a scoped set of files inside an isolated
  container, the project's checks run offline, a *different* provider reviews the exact diff, and the
  change is published as a pull request from a fork. Preparing a pull request grants no permission to
  merge or deploy it.
- **A signed release gateway.** Production only applies a release manifest that a host-side verifier
  checks against a signed policy and a release ledger. Low-risk changes (currently documentation only)
  can be released by the gateway under that policy; everything else needs a founder signature.
- **Business feedback.** Collectors record metrics with their source, unit and observation time. Goals,
  experiments and task context use those measurements; missing data stays unknown, never zero.
- **Operations.** A watchdog, deduplicated notifications, encrypted off-site backups with restore tests,
  and a Founder Desk web UI that shows the real state of the system.

## Architecture

```mermaid
flowchart LR
    subgraph Sources
        M[Metric collectors] 
        W[Watchdog]
        F[Founder Desk]
    end
    subgraph Core["Company OS API (FastAPI)"]
        DB[(PostgreSQL<br/>operational state)]
        R[Business reconciler<br/>goal evaluation]
        T[Task materializer]
    end
    subgraph Execution
        WK[Unattended worker]
        P[Providers<br/>Claude / Codex CLI]
        SC[Stage C coder<br/>isolated container]
        RV[Independent reviewer<br/>other provider]
    end
    subgraph Delivery
        PR[Pull request<br/>from a fork]
        CI[GitHub Actions<br/>exact-head CI]
        GW[Release gateway<br/>signed manifest]
        H[Host verifier<br/>+ release ledger]
    end
    M --> DB
    W --> DB
    F <--> DB
    DB --> R --> T --> DB
    DB --> WK --> P
    WK --> SC --> RV --> PR --> CI
    CI --> GW --> H
    H --> DB
```

| Component | Where | Role |
|---|---|---|
| API | `app/main.py` | State, tasks, events, approvals, Desk endpoints |
| Worker | `app/worker.py` | Claims eligible tasks, runs providers, checkpoints results, recovers after crashes |
| Providers | `app/providers.py` | One class per provider; no metered API fallback |
| Reconciler | `app/business_reconciler.py`, `app/goals.py`, `app/goal_evidence.py` | Turns measured goal gaps into deduplicated tasks |
| Stage C | `app/stage_c_worker.py`, `app/stage_c_runner.py`, `app/stage_c_ci.py` and the other `stage_c` modules | Isolated coding, offline checks, independent review, fork PR, CI observation |
| Release gateway | `app/release_gateway.py`, `app/release_gateway_service.py`, `app/release_host.py`, `app/review_signer.py` | Signed, host-verified releases |
| Founder Desk | `desk/` (React + Vite), built into `app/static/` | The web UI |
| Agents | `agents/` | Agent identities, permissions, cost and release policy |

Design documents: [docs/AUTONOMOUS_AGENT_ARCHITECTURE.md](docs/AUTONOMOUS_AGENT_ARCHITECTURE.md),
[docs/AGENT_EXECUTION.md](docs/AGENT_EXECUTION.md), [docs/CODER_RUNTIME.md](docs/CODER_RUNTIME.md),
[docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md).

## Agent lifecycle

```
GOAL → TASK → ELIGIBILITY → CODER → TESTS → INDEPENDENT REVIEW → PR → CI
     → AUTHORIZED DELIVERY → VERIFICATION → LEARNING → NEXT TASK
```

Every transition is a database state change with an owner and evidence. Retries are bounded, a
provider quota limit means waiting (never paying for a fallback), and an agent never approves its own
work. A successful model response is not a passed test, and a passed CI run is not a verified release.

## Autonomy levels and approval boundaries

| Stage | Scope | Default |
|---|---|---|
| A | Observe and report (watchdog, briefs, Desk) | on |
| B | Automatic low-risk tasks with no tools and an empty scratch directory | on for opted-in tasks |
| C | Reversible engineering in an isolated worktree, producing a reviewed pull request | off until enabled per task |
| D | Selected production workflows behind approval gates | not enabled |

The human owner (called *the founder* in this codebase) remains the authority for strategy, spending,
publishing, production deploys, credentials and policy changes. Automation flags default to off, and a
pause stops autonomous execution without stopping production services. See the Stage C security design in
[docs/AUTONOMOUS_AGENT_ARCHITECTURE.md](docs/AUTONOMOUS_AGENT_ARCHITECTURE.md) and
[docs/APPROVAL_IDENTITY.md](docs/APPROVAL_IDENTITY.md).

## Technology stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic, Uvicorn
- **Database:** PostgreSQL 18
- **Frontend:** React, TypeScript, Vite, Tailwind CSS (`desk/`)
- **Runtime:** Docker and Docker Compose; separate images for the API, worker, coder, reviewer,
  review signer and an egress proxy (`Dockerfile`, `docker-compose.yml`)
- **Tests:** pytest with pytest-xdist; Node's test runner for the frontend
- **Backups:** `pg_dump`, `age` encryption, S3-compatible object storage

## Quickstart

This runs the API and the Desk locally against a disposable PostgreSQL. It needs Python 3.12 and Docker.

```bash
# 1. A disposable database (development only)
docker run -d --name seerxo-dev-db -e POSTGRES_USER=company -e POSTGRES_PASSWORD=dev-only \
  -e POSTGRES_DB=company -p 127.0.0.1:55432:5432 postgres:18-alpine

# 2. Dependencies
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. Schema and an example organisation
export DATABASE_URL="postgresql+psycopg://company:dev-only@127.0.0.1:55432/company"
export ENVIRONMENT=development
alembic upgrade head
python -m scripts.seed

# 4. Run the API
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open:

- `http://127.0.0.1:8000/`: the Founder Desk
- `http://127.0.0.1:8000/docs`: the interactive API reference
- `http://127.0.0.1:8000/health`: a health check

`scripts/seed.py` creates a small fictional company so the Desk has something to show. Replace it with
your own organisation. To rebuild the Desk after changing `desk/`, run `npm ci --prefix desk` and
`npm run build --prefix desk`.

The worker, the coder and the release components need provider logins, container isolation and a
host setup that this quickstart does not create. Read [docs/CODER_RUNTIME.md](docs/CODER_RUNTIME.md)
before enabling them.

## Configuration

All configuration comes from environment variables; `app/config.py` reads them and never from a file
committed to Git. [`.env.example`](.env.example) lists every variable with a harmless placeholder.
Copy it to `.env` (which is ignored by Git), fill in your own values and keep the file private
(`chmod 600 .env`). Empty notification or storage credentials disable that integration; events are
still recorded.

Never commit a real value. Secrets are referenced by name only in code, tasks, prompts and logs.

## Running the tests

The backend suite needs a disposable PostgreSQL whose database is named
`company_external_release_test`; the tests refuse to run against any other name.

```bash
docker run -d --name seerxo-test-db -e POSTGRES_USER=release_test -e POSTGRES_PASSWORD=disposable-ci-only \
  -e POSTGRES_DB=company_external_release_test -p 127.0.0.1:55433:5432 \
  --tmpfs /var/lib/postgresql postgres:18-alpine

export EXTERNAL_RELEASE_TEST_DATABASE_URL="postgresql+psycopg://release_test:disposable-ci-only@127.0.0.1:55433/company_external_release_test"
python -m pytest -q -n 4 --dist loadgroup
```

Some tests exercise the release and archive tooling and need a Git checkout (not an exported tree) and
Node.js. The exact commands CI runs are in
[`.github/workflows/python-tests.yml`](.github/workflows/python-tests.yml).

Frontend:

```bash
npm ci --prefix desk
npm test --prefix desk
npm run lint --prefix desk
npm run build --prefix desk
```

## Security and trust boundaries

- **Preparing a pull request is not permission to merge or deploy it.** The coding agent works in an
  isolated container without GitHub credentials, its account has read-only access to the upstream
  repository, and it publishes only to a fork.
- **Independent review.** Code written by one provider is reviewed by another; the reviewed diff is
  bound to the published commit by a fingerprint.
- **Signed releases.** Production applies only a manifest that the host-side verifier checks against a
  founder-signed policy and an append-only ledger. A merge to `main` does not deploy anything by
  itself.
- **No secrets in the repository.** Production configuration lives on the protected host or in the
  deployment platform, never in Git. No workflow in this repository names a secret.
- **Pull request code never runs on persistent infrastructure.** All CI runs on fresh GitHub-hosted
  virtual machines with a read-only token.
- **Tripwires, not boundaries.** `tests/test_trust_boundary.py` inventories what a repository change
  can reach. Anyone who can edit the repository can edit those tests, so the real boundaries are the
  host verifier, the signed policy and the credential separation.

Details: [docs/security/THREAT_MODEL.md](docs/security/THREAT_MODEL.md),
[docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md),
[docs/REPOSITORY_GUARDIAN.md](docs/REPOSITORY_GUARDIAN.md).

## Production deployment concepts

A production installation is a single host running the Compose services, reachable only through an
authenticated tunnel, with the database on an internal network and no public ports. Releases are
built from an exact commit, staged, verified by the host-side verifier against the signed policy, and
recorded in the release ledger; a failed verification refuses the release and a failed apply rolls
back to anchored images. Backups are encrypted before they leave the host, and restore tests run on a
schedule. This repository contains the tooling, not a deployment: no production addresses or credentials (test fixtures use example private-network addresses; the operator scripts reach the host through an SSH alias named `company-os-host`; most accept `COMPANY_SERVER`, `COMPANY_OS_DEPLOY_HOST` or `HOST`, and the Stage C host wizard's examples use the alias directly).

## CI

Every workflow runs on GitHub-hosted `ubuntu-24.04` runners with a read-only token and no secrets:

| Workflow | Purpose |
|---|---|
| `python-tests` | Backend suite and PostgreSQL, concurrency and crash-recovery tests (required) |
| `frontend` | Desk build, tests and lint, and a check that the built bundle is committed (required) |
| `secret-scan` | TruffleHog over the pull request's commits |
| `dependency-scan` | OSV scan of Python and npm dependencies |
| `workflow-lint` | zizmor and actionlint |
| `prowler` | Infrastructure-as-code scan |
| `ci` | A manually started readiness check for one exact `main` commit; it deploys nothing |

Third-party actions are pinned to full commit SHAs.

## Project structure

```
app/          FastAPI application, worker, reconciler, Stage C, release gateway
desk/         Founder Desk frontend (React, TypeScript, Vite)
migrations/   Alembic migrations
agents/       Agent registry, cost policy, release policy, repository guardian policy
skills/       Reusable agent procedures
scripts/      Operator CLI (companyctl), backup and restore, deploy and release tooling
tests/        pytest suite
evals/        Golden evaluations for agent behaviour
ops/          Compose overlays
docs/         Design documents
```

## Development workflow

Trunk-based: `main` is the only permanent branch, every change goes through a pull request from a
short-lived branch, and required checks must pass on the exact head commit. See
[docs/GIT_WORKFLOW.md](docs/GIT_WORKFLOW.md). Coding agents follow [CLAUDE.md](CLAUDE.md) (also
available as `AGENTS.md`).

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md) before opening a pull request.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting as described in
[SECURITY.md](SECURITY.md).

## Project maturity and known limitations

- **V1.** The API, worker, reconciler, Desk, backups and release gateway run in production for one
  company. Stage C has completed an attended pilot; unattended operation is not enabled by default.
- **Automatic merge is not enabled.** Merging remains a human decision.
- **Single-host design.** The reference deployment is one host; there is no horizontal scaling.
- **Opinionated providers.** The reference providers drive command-line tools tied to subscription
  logins; other providers need a new provider class.
- **Documentation.** Some design documents describe decisions made for the original deployment and use
  placeholders such as `<production-host>` where an address was removed.

## License

No license has been chosen yet. Until a license file is added, all rights are reserved: you may view
and fork the code on GitHub, but no permission to use, modify or distribute it is granted.
