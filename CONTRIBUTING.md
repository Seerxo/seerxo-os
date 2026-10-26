# Contributing to Seerxo OS

Thank you for your interest. This document explains how to propose a change.

## Contributions and licensing (please read first)

No license has been chosen for this project yet (see [README.md](README.md#license)). Until a license
and a contributor agreement are published, **code and documentation pull requests from outside the
maintainer team cannot be accepted**: a contribution would remain under its author's copyright, and
the project would then need every contributor's permission to choose or change its license.

What is welcome now: bug reports, security reports, questions and design discussion in issues. Once
a license and contribution terms are published, this section will be updated.

## Before you start

- Read [README.md](README.md) for what the project does and how to run it.
- For a large change, open an issue first so we can agree on the approach before you write code.
- Security problems go through [SECURITY.md](SECURITY.md), never a public issue.

## Workflow

The repository is trunk-based ([docs/GIT_WORKFLOW.md](docs/GIT_WORKFLOW.md)):

1. Fork the repository and create a short-lived branch from the current `main`, named
   `<type>/<short-slug>` where type is one of `feat`, `fix`, `docs`, `test`, `ci`, `chore`, `refactor`,
   `perf`, `hardening` or `security` (for example `fix/worker-lease-renewal`).
2. Make the smallest change that solves one problem.
3. Add or update tests. A bug fix should come with a test that fails without it.
4. Run the tests locally (see [Running the tests](README.md#running-the-tests)).
5. Open a pull request against `main`. Its title is a Conventional Commit subject, for example
   `fix(worker): renew the lease before the provider call`.

## Pull request expectations

- Describe what changed, why, which tests you ran, and anything left open.
- Required checks must pass on the exact head commit: `python-tests` and `frontend`.
- Keep pull requests focused; prefer under about 400 changed lines.
- Do not commit secrets, real credentials, production addresses, personal data or database dumps.
  Use `.env.example` style placeholders.
- Changes to `.github/workflows/`, the release gateway, the host verifier, the review signer, the
  agent policies in `agents/` or `tests/test_trust_boundary.py` receive an extra security review.

## Coding style

- Python 3.12, standard library first; add a dependency only when it removes real work.
- Deterministic code decides whether work exists; a model is called only for judgment.
- Bounded retries, explicit states, and evidence for every claim of success.
- Comments explain *why*, not *what*.

## AI-assisted contributions

Contributions written with AI assistance are welcome. You are responsible for the change: review it,
run the tests, and make sure it does not weaken a security check or a test to make it pass.

## Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
