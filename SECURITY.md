# Security policy

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. This creates a private advisory that only the maintainers can see.

Include, where you can:

- the affected component and file (for example `app/release_host.py`),
- the commit you tested,
- steps to reproduce or a proof of concept,
- the impact you expect (what an attacker gains).

We will acknowledge the report, keep you informed while we investigate, and credit you in the advisory
if you want. Please give us reasonable time to fix the problem before you disclose it.

## Supported versions

Only the latest commit on `main` is supported. There are no maintained release branches.

## Scope

In scope:

- the API, worker, reconciler and Founder Desk (`app/`, `desk/`),
- the Stage C coding pipeline, the review signer and the release gateway and host verifier,
- the backup, restore, deploy and operator scripts (`scripts/`),
- the GitHub Actions workflows (`.github/workflows/`).

Out of scope:

- vulnerabilities in third-party dependencies that are already public (please still tell us if a
  dependency is exploitable through this project),
- reports that need physical access to a machine or a compromised maintainer account,
- findings from automated scanners without a demonstrated impact.

## How this repository is protected

- No secrets are stored in the repository and no workflow uses a repository secret.
- Pull request code runs only on fresh GitHub-hosted runners with a read-only token.
- Coding agents publish only to a fork and cannot merge; production applies only releases that a
  host-side verifier checks against a signed policy. See
  [docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md) and
  [docs/security/THREAT_MODEL.md](docs/security/THREAT_MODEL.md).
