# Gateway releases, legacy readiness and production verification

## A merge does not deploy

PR/main CI runs in the Python/PostgreSQL and frontend workflows listed in
`agents/repository_guardian.yaml`. All configured exact-commit checks remain required.

The old `ci.yml` SSH deploy job is retired. Its remaining `deploy-authorization`, `ci-gate` and
`deploy-provenance` jobs are a manual, read-only readiness check. They name no deploy secrets,
write no SSH material and invoke no deploy driver. A successful dispatch releases nothing.

- Readiness dispatch must be from `main`, by the founder, for the exact dispatched main tip.
  Its code comes from that trusted ref. The optional migration revision is format-checked only;
  it grants no production migration authority.
- `ci-gate` still checks every required workflow and any configured trusted-publisher statuses
  for the exact commit. Missing, pending, failed, spoofed or other-commit evidence refuses readiness.
- These manual jobs still name the legacy self-hosted runner. Keep the workflow disabled during
  [B6 cutover](../security/LEGACY_DEPLOY_CUTOVER.md), before retiring that runner. This source change
  does not disable the workflow, revoke a key, delete GitHub secrets, or remove runner registrations.
- Production releases use the root-owned installed `release_host` outside the deploy payload.
  It checks the signed policy, manifest, independent review, payload, production identity and ledger
  before applying. HIGH changes and migrations require the founder's signature. See
  [RELEASE_GATEWAY.md](../security/RELEASE_GATEWAY.md).

The source invariant is only one B6 condition: real credential/runner revocation and negative
host tests are also required. Do not mark B6/H3 complete from a passing repository test.

The driver/recovery guarantees below remain relevant to the host release path. A corrected
source driver or verifier is not proof of an installed runtime; the founder records exact
installed identities and a verified release separately.

## Clean build context, failure restore and interrupted deploys

- **Images are built from a clean copy of the release.** The host overlays the archive onto `/opt/company-os` and
  keeps files a release no longer ships, and the Dockerfile copies whole `app/` and `migrations/`. The driver
  therefore extracts `src.tgz` into `backups/deploy-<S>/build-context`, verifies it byte-for-byte against the payload
  manifest (`sha256sum -c`, and the file set must equal the manifest's), then runs `docker build --target <t>` for
  the four services from that directory and deletes it. A stale module or Alembic revision on the host can no longer
  reach an image (the old host-tree build turned a leftover revision into the image's Alembic head). Host-only files
  stay on the host and are listed as `HOST-ONLY`; nothing is deleted. The build reproduces only a plain
  `{context: ".", target: T}` compose build; any other build option stops the deploy (restored, containers untouched).
  This replaces the abort-on-stale-files approach of `aad97c4`: the image is clean by construction, so the host tree no
  longer has to be.
- **Any failure before the container switch restores the previous release**: source bytes (and files the release
  newly added are removed again, `files-new.list`), the default image tags of all four services, the host CLI, and
  leaves the cron schedule and containers untouched. Only the release's own `scripts/*.sh` get the executable bit;
  host-only scripts are not modified.
- **Deploy journal.** `backups/deploy-<S>/phase` records the phase atomically: `SOURCE_INSTALLING`, `BUILDING`,
  `BUILT`, `MIGRATING`, `MIGRATED`, `CLI_INSTALLING`, `SWITCHING`, then a terminal one: `VERIFIED`,
  `ROLLED_BACK_VERIFIED`, `ABORTED_RESTORED` (clean) or `ROLLED_BACK_PARTIAL`, `ROLLBACK_FAILED` (need a human). Any
  non-clean phase blocks the next `--apply` (the read-only run lists it as `UNFINISHED`).
- **Recovery after an interruption (killed deploy, lost SSH host, power loss):**
  `sudo RECOVER_DIR=/opt/company-os/backups/deploy-<S> bash /opt/company-os/backups/deploy-<S>/deploy-driver.sh`
  restores source, host CLI, cron, tags and containers from the backup, verifies with the release health gate, and
  prints `RECOVERY VERIFIED: <old sha>`; running it again prints `NOTHING TO RECOVER`. `deploy.env` is read through an
  allow-list and regexes, never sourced. A migration that already ran is **not** reverted (the schema stays at the new
  revision; migrations are additive); the next deploy treats that revision as current. Restoring the database is a
  separate attended decision using `db.dump` in the same directory.
- Acceptance tests: `tests/test_release_acceptance.py` runs the real driver script against a disposable sandbox host
  with a fake `docker` (images, containers, alembic revision, failure and crash switches). It is evidence about the
  script's logic, **not** about production, Docker, PostgreSQL or the real health gate.

## Read-only production verification plan

Run after any deploy, or to establish the current state. Every command only reads; none uses `--apply`, none
writes, restarts, migrates or changes a file. Run privileged host checks from the independent founder admin session (Touch ID), not the agent-reachable semih account. Record the output; do not paste secrets. Anything not run is
**UNKNOWN**, not assumed.

Set `SHA` to the commit you expect (normally `git rev-parse origin/main` after `git fetch`).

| # | Check | Command (read-only) | Expect |
|---|---|---|---|
| 1 | Deployed SHA, API | `ssh <admin-host> "sudo docker exec company-api printenv COMPANY_OS_GIT_SHA"` | equals `$SHA` |
| 2 | Deployed SHA, worker | `ssh <admin-host> "sudo docker exec company-worker printenv COMPANY_OS_GIT_SHA"` | equals `$SHA` |
| 3 | Last deploy outcome | `ssh <admin-host> "sudo tail -n 5 /var/log/company-os-deploy-latest.log"` | `RELEASE VERIFIED: $SHA` and `EXIT 0` (never `ROLLBACK`/`ABORT`) |
| 4 | Alembic revision on the host | `ssh <admin-host> 'sudo docker exec company-postgres sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -Atc \"select version_num from alembic_version\""'` | equals the revision below |
| 5 | Expected revision for `$SHA` | in a clean checkout of `$SHA` (e.g. `git worktree add /tmp/sha-check $SHA`): `DATABASE_URL=postgresql+psycopg://unused@127.0.0.1:1/unused python3 -m alembic heads` (offline: it never connects) | prints exactly one line ending `(head)`; an empty result or several heads is a failed check, not a pass |
| 6 | Does `$SHA` ship a schedule? | `git cat-file -e $SHA:scripts/company-os.cron && echo present \|\| echo absent` | `present` or `absent` (both valid) |
| 7a | If present: installed checksum | `ssh <admin-host> "sha256sum /etc/cron.d/company-os; stat -c '%U:%G %a' /etc/cron.d/company-os"` | checksum equals 7b; `root:root 644` |
| 7b | If present: expected checksum | `git show $SHA:scripts/company-os.cron \| shasum -a 256` | (reference value) |
| 7c | If absent: no live schedule | `ssh <admin-host> "test ! -e /etc/cron.d/company-os && echo absent"` | `absent` (a release without a schedule removes it) |
| 8 | No half-installed schedule | `ssh <admin-host> "ls -la /etc/cron.d"` | no `.company-os.new.*` leftover |
| 9 | Release file drift (stdin only) | `git ls-tree -r $SHA --format='%(objectname) %(path)' -- app migrations scripts agents skills tests alembic.ini requirements.txt Dockerfile docker-compose.yml docs/PERMANENT_FACTS.md \| while read -r o p; do printf '%s  %s\n' "$(git cat-file -p $o \| shasum -a 256 \| cut -c1-64)" "$p"; done \| ssh <admin-host> "cd /opt/company-os && sudo sha256sum --quiet -c -"` | no output, exit 0 (only mismatches are printed; host-only files are not reported) |

Check 9 deliberately does not run `scripts/deploy-company-os.sh`: even without `--apply` that script copies files to
the host's `/tmp` before its host-side preview, which is a write.

Reading the results:

- Checks 1 and 2 differ from `$SHA`: production is not on that commit. Not an error by itself (nothing deploys on
  merge); it is the answer to "what is running".
- Check 4 differs from check 5: a schema change is either pending (host behind) or was applied that this commit
  does not contain. Do not run a migration to "fix" it; escalate to the founder.
- Check 7a differs from 7b (or 7c shows a file): the host schedule is not this commit's. If checks 1 to 3 show a
  verified deploy of `$SHA`, that is a defect to report; if not, it is the previous release's schedule. When check 6
  says `absent`, a missing live file is the expected result, not a fault.
- Check 3 ending in `ROLLBACK FAILED` or `PARTIALLY ROLLED BACK` needs manual recovery from the backup directory the
  log names.

## Status of the verification plan

Each deployed SHA needs fresh founder-run evidence for all nine checks. Historical snapshots do not
prove current state. The agent-reachable semih account is expected to be denied sudo and Docker after P0 isolation;
do not restore its privileges to collect evidence.
