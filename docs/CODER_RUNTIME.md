# Stage C coder runtime: container, egress proxy, launcher

## Current status — source + host facts, 2026-10-07

The Stage C implementation, host launcher, proxy, images, worker IPC and Claude/Codex role split
are deployed. `companyctl stage-c preflight` is still **NOT READY** because the founder has not
recorded a current-SHA host acceptance: real Claude edit, Codex review probe, offline checks and
isolation evidence remain un-attested. `ENABLE_AUTONOMOUS_CODE_WRITE` remains **OFF**. No pilot,
Guardian activation or autonomous code admission is authorized until that gate is proven.

## Historical status (2026-10-05)

Status (2026-10-05): **approved** by the founder (§122) · **implemented in the repo** (#83) ·
**not deployed** · **not accepted** on the host · **worker wiring prepared (see §5)** · **disabled**.
`ENABLE_AUTONOMOUS_CODE_WRITE` stays false and the worker stays Stage B until the
enablement order at the end is complete. New spend: $0. Everything runs on the existing
production host and the existing Claude and ChatGPT subscriptions.

Builds on: `AUTONOMOUS_AGENT_ARCHITECTURE.md` §12 (boundaries table), `CODER_SANDBOX_PROOF.md`
(offline profile), `CODER_SOURCE_EXPORT.md` / `CODER_DIFF_IMPORT.md` (what crosses in and out),
`app/stage_c_runner.py` (`coder(workspace, job)` and `checks(directory)` callbacks).
This note only settles what those leave open: who starts the containers, which login they
use, how egress is limited, and the exact CLI invocation.

## 1. Shape

### Local founder-pause closure (not installed or host-accepted)

The launcher queries the existing `agents_paused` control row through a fixed, unprivileged
`docker exec --user=10001:10001 company-api` helper. Database credentials remain inside that
trusted control-plane container. Only PostgreSQL and an existing literal `paused: false`
record permit admission; missing, malformed, paused or unavailable state refuses the request.

The helper holds the control-row lock through startup. The trusted container wrapper signals
`started` but cannot execute the coder/checks command until root writes `allowed`. Root asks
the helper to recheck the locked row immediately before granting that permission, then closes
the helper and releases the job launch lock. A founder pause either commits first and refuses
the queued request, or follows this already-admitted start. Cancellation is checked under the
same job lock again before permission is granted. An uncertain admission consumes the start
allowance conservatively; it never authorizes an automatic retry as a known non-start.
The wrapper exits after a bounded wait if root disappears before granting permission.

Pause stops new admissions. An already admitted job may finish within its existing code/checks
timeout; this does not grant permission to publish or release. The worker's subsequent pause,
ownership and authorization checks still apply. This patch does not terminate production
services or promise immediate cancellation of an already running model call.

Refusals use bounded static reasons in the existing result and systemd journal paths; the
helper's raw database stderr is discarded. Local negative tests cover the launch boundary
and wrapper; real PostgreSQL serialization and Docker/UID/filesystem isolation still require
the separately authorized host acceptance. The helper uses bounded queries and a 60-second
idle transaction timeout to avoid keeping the pause row locked after a dead launcher.

The DB helper alone does not close admission: losing that process can release its row lock
after `ALLOW`. The launcher therefore holds a root-owned file lock from before the query
through creation of `ack/allowed`. The founder control endpoint takes the same lock through
its DB commit. A helper crash cannot move a successful pause commit ahead of the permission
grant. A durable start marker is written before permission and retained through result
publication. A completed, proven pre-grant refusal removes it. Process death before the
grant can conservatively consume an allowance even when no model ran; death after the grant
cannot turn an executed command into a known non-start. This favors bounded use over retries.
The worker acquires and rechecks the job launch lock before releasing its task-owner fence
on that marker. A marker visible during root's handoff cannot admit a task cancellation or
reassignment before the execution grant; a lock timeout is an unknown start, never a safe retry.

**Separate attended installation gate:** with Stage C OFF, drain requests and stop the old
launcher timer; provision `/etc/company-coder/pause.lock` as a regular, root-owned `0444`
file in the existing root-only directory. Apply `ops/company-coder-pause.compose.yml` with
the new API image: it mounts this exact inode read-only at `/run/company-coder-pause.lock`
and enables the fence on the sole founder pause write path. Install the reviewed launcher
only with separate approval. It checks the API's device/inode identity and refuses a legacy
API, missing mount, writable/symlink/hardlinked fence, unknown pause or unavailable DB.
Never unlink or replace this file while either component is running. The coder/checks and
worker receive no fence mount. No DB credential leaves the API container.

The endpoint returns HTTP 503 if the fence is unavailable or remains busy for ten seconds;
this means the pause was **not applied**, so the founder must retry and confirm the state.
Host acceptance must exercise both ordering directions, helper death, launcher death before
permission, a queued request after a committed pause and the actual read-only bind identity.
The local tests do not prove those Docker/PG/host properties. A rollback keeps Stage C OFF
and the launcher timer stopped before reverting either component or removing the mount.

```
company-worker (container; DB creds; no Docker socket, no model for Stage C)
  │ stage_c_runner.run(): export scoped files → /srv/company-coder/jobs/<KEY>/workspace
  │ writes jobs/<KEY>/request.json  {kind: code|checks, provider, prompt, timeout_s}
  ▼
company-coder-launch (host, systemd poll timer, root; fixed profiles, no other input)
  │ validates <KEY> and request.json, runs ONE container with a fixed profile:
  │
  ├─ kind=code   → company-coder image, network coder-net (internal) → egress proxy
  │                mounts: workspace rw, that provider's coder login volume rw
  └─ kind=checks → project test image, network none, mounts: checks copy rw
  │ writes jobs/<KEY>/result.json {exit, timed_out}; removes the container
  ▼
company-worker: import_changes() (untrusted) → checks → review → publish (unchanged)
```

## 2. Decisions

**D1. Containers are started by a host launcher, never by the worker.** Docker socket access
is root on the host, and a socket proxy still allows arbitrary mounts. Instead, the worker
drops a request file, and a root-owned host script (`scripts/company-coder-launch`, a
systemd poll timer) runs one of two hard-coded `docker run` profiles. A code request is
Claude-only; the request cannot add a mount, an env var, a flag or an image.
The key is validated against `TASK-\d{4,}` and every path is built by the launcher. The worker
sees only `/srv/company-coder/jobs` (bind mount); the launcher reads only that.

**D2. Coder and reviewer logins are separate, least-privilege subscription sessions.**
- Claude is the only Stage C coder. Its headless `claude setup-token` is kept in a root-only
  host file (`/etc/company-coder/claude.env`, 0600) and passed to Claude runs only. A normal
  interactive Claude login stores CLI credentials in the CLI profile instead; the wizard
  uses the setup-token path for the isolated coder.
- Codex is the independent reviewer in `company-reasoner`, not a Stage C coder. Its ChatGPT
  subscription OAuth lives in the writable persistent volume `company-worker-codex`, mounted
  only at `/home/worker/.codex` in `company-reasoner`. The reviewer refreshes this `auth.json`
  in place. It is not copied to `company-coder-codex` or mounted into the coding sandbox.
- The wizard accepts Codex device login only after verifying the running reasoner mount and
  requires a founder-confirmed interactive login. If device-code login is disabled by the
  ChatGPT account/server, it stops with guidance for supported browser OAuth. It never
  falls back to an API key. The official CLI exposes this as `codex login --device-auth`;
  a server that returns “device code login is not enabled” requires an account-policy or
  interactive browser-login resolution, not API-key billing. See the [Codex CLI device flow](https://github.com/openai/codex/blob/main/codex-rs/login/src/device_code_auth.rs).
- An auth file or `claude auth status` proves credential configuration only, not model access.
  The acceptance wizard separately asks before one real Claude coder edit and one bounded
  Codex reviewer model probe; their results are bound to the accepted release SHA.
- Anthropic currently documents Claude Code access for individual Pro/Max plans and says
  the Team plan does not include Claude Code. An entitlement/policy denial for a Team login
  is distinct from an expired setup-token; use only an already-authorized Claude Code
  subscription and do not switch to metered API billing. See [Claude Code setup](https://docs.anthropic.com/en/docs/claude-code/getting-started)
  and [Team plan access](https://support.anthropic.com/en/articles/9266767-what-is-the-team-plan).
- Only the job's provider credential is mounted or passed. Metered keys (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `CODEX_API_KEY`, …) are never set, and the launcher refuses to start if
  the env file contains one (§26A).

**D3. Egress goes through one allow-list proxy: tinyproxy** (Debian package, GPL, small config).
- Docker network `coder-net` with `internal: true`: no route out. The coder joins only
  this network. The proxy joins `coder-net` and a normal bridge network, so it is the only
  way out.
- `FilterDefaultDeny Yes`, `ConnectPort 443` only, a filter file with exact hosts.
  Starting list:
  - Claude: `api.anthropic.com`
  - Codex: `chatgpt.com`, `auth.openai.com`

  Telemetry is switched off instead of allowed (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
  Codex telemetry hosts left denied). Hosts the CLIs actually request are taken from the
  proxy log during acceptance (step 4). Each extra host is a reviewed change to this file.
  Denied by default: GitHub, PyPI, npm, SSH, everything else.
- Both CLIs read `HTTPS_PROXY` (Claude Code network config, Codex network-proxy docs).

**D4. Coder invocation. The container is the security boundary, not the CLI's tool policy.**
Checks run in a separate, network-less container anyway (D5). Claude gets edit tools only
(no `Bash`). The host launcher independently refuses every Stage C `provider=codex` request;
the Codex CLI and reviewer volume are never mounted in the coder sandbox. Codex review runs
in `company-reasoner` through its own service and egress path, without a task workspace or
control-plane credentials.
- Claude:
  `claude -p --tools Read,Edit,Write,Glob,Grep --permission-mode acceptEdits --max-turns 30 --no-session-persistence --output-format json`
  **Decision:** Codex remains reviewer-only. Earlier sandbox attempts failed under the
  hardened coder profile; no sandbox exception or privilege expansion is authorized.
  Acceptance uses `codex_review_probe` through the independent reasoner path.

**D5. Checks run model-written code, so they get the offline profile.** This is the
`CODER_SANDBOX_PROOF.md` profile with only the image and limits changed: a per-project test
image with dependencies pre-installed (for sez-company, the existing `api` image target,
which has `requirements.txt`), `--network=none`, read-only root, tmpfs `/tmp`, uid 10001, no
env. The command comes from the repo profile (sez-company: `python -m pytest -q`), never from
the model.

**D6. Profiles and limits** (both kinds): `--pull=never` with an image ID pinned by the
launcher, `--read-only`, `--user=10001:10001`, `--cap-drop=ALL`,
`--security-opt=no-new-privileges:true`, `--pids-limit=256`, `--memory=2g`, `--cpus=1`, private
PID/IPC, no ports. Wall clock: code 20 min, checks 10 min, enforced by the launcher with
`timeout` + `docker rm -f`. At most one Stage C container at a time (the repository lease
already serialises; the launcher is a systemd oneshot unit, so requests run one after another). The host has 4 vCPU.
Production keeps priority.

## 3. Not in v1

- Bash for Claude, package installs, or a Stage C Codex coding path. These require a separate
  reviewed security decision; Codex remains reviewer-only.
- More than one repository. Start with `sez-company`, LOW risk, a bounded file list.
- Guardian enablement. It stays `enabled: false` until a real pilot PR has gone through
  CI and post-merge verification.

## 4. Enablement order (each step is a separate PR or founder action)

1. Founder approves this note. Done 2026-10-05.
2. Repo (PR): `Dockerfile` target `coder` (the pinned CLIs, no app code), `docker-compose`
   service `company-egress` + network `coder-net`, `scripts/company-coder-launch` + systemd
   units, `stage_c_runner.launcher_coder()` / `launcher_checks()` adapters. Tests run in CI
   (request validation, refused metered env, fixed argv). No server change.
   Done: #83.
3. Founder on the host: deploy with docker-compose.yml shipped (#86), build images, provision
   the Claude setup-token file, verify Codex's existing `company-worker-codex` reviewer volume,
   install the systemd units, and start the proxy.
4. Acceptance on the host, recorded in this file:
   - the sandbox proof extended to the coder profile;
   - from inside `coder-net`, a denied host and a direct connection both fail;
   - Claude completes one fixed no-op edit through the coder launcher;
   - after a separate confirmation, Codex answers one bounded reviewer probe through the reasoner;
   - the proxy log lists every host contacted;
   - whether Codex's Landlock sandbox starts under `no-new-privileges` (D4 open point).
5. Worker IPC (PR): `company-worker` gets the bind mount `/srv/company-coder/jobs`, owned by
   uid 10001, as the only shared path (in compose since this step's PR), and `run`'s workdir
   points at it (with the wiring, step 6).
6. Golden tasks on real providers. Then wire the worker behind `ENABLE_AUTONOMOUS_CODE_WRITE`
    for sez-company LOW risk only (separate approval). Wiring is prepared behind the
    existing OFF flag; preparing code is not host acceptance or activation.

Rollback at any step: stop the timer and the proxy, remove the coder volume and the
token file (`claude` token revoked from the account page). No migration, nothing touches
production services.

## 5. Worker wiring contract and remaining operational gate (2026-10-06)

`app/stage_c_worker.py` only connects the existing runner, launcher adapters, independent
review, publisher, CI ingestion and verification. No second execution engine was added.
Admission requires the write flag, literal `autonomous: true` / `write: true`, LOW risk,
no approval, an ACTIVE project (and active parent unit), the allowlisted repository and
its explicit `main` branch in `agents/repository_guardian.yaml`, and a bounded exact list:

```json
{"autonomous": true, "write": true,
 "stage_c": {"paths": ["tests/test_metrics.py"]}}
```

The initial known scope is deliberately small: `app/goals.py`, `app/metrics_store.py`,
`app/packaging.py` and their existing tests (`tests/test_goals.py`, `test_metrics.py`,
`test_packaging.py`). Other scope requires a reviewed policy change. No new, renamed or
deleted files; no migration, auth/billing, governance, workflow, deploy or destructive
operation. Independent review also rejects semantic changes in those categories and
weakened checks. Success criteria and scope are rechecked before publishing.

The repository and mirror are control-plane constants. The host launcher still sees only
the existing `jobs/<TASK>/workspace` or `checks` directories. Prepare a worker-owned bare
mirror at `/srv/company-coder/jobs/.control/sez-company.git` (uid/gid 10001); the worker
fetches `main` from the fixed GitHub URL and pins each task's original base across repairs.
`git` and `gh` are included in the worker image. A founder-provisioned fine-grained token,
restricted to this repository (contents/PR write, Actions read; no workflow/admin permission),
comes from host `COMPANY_STAGE_C_GITHUB_TOKEN`, mapped to worker `GH_TOKEN`. It never
reaches the provider subprocess environment or either launcher profile. Do not display it.
Unfinished requests, changed published branches or ambiguous push/PR failures block for
the founder instead of being destroyed or blindly retried.

Before admission, the founder must record actual step-4 evidence in the existing
`control_state` table: key `stage-c-host-acceptance`, `updated_by=founder`, value containing
the **full deployed** `source_sha` and a `checks` object with each of these booleans true:
`image_build`, `egress`, `launcher`, `permissions`, `direct_egress_blocked`, `github_blocked`,
`random_hosts_blocked`, `provider_hosts`, `claude_edit`, `codex_review_probe`,
`offline_checks`, `credential_isolation`. Keep detailed non-secret acceptance evidence in
Git. No agent sets this attestation, and a different worker source requires new acceptance.
Missing acceptance, credentials, mirror or a different review provider means wait before
claim; quota/rate/auth failures cool down without spending a task retry. Launcher polling
keeps the worker/agent heartbeat current; a Stage C run gets a one-hour stale-run boundary.

Use a pinned **worker image** for `checks_images.sez-company`: the full Python suite needs
git and Node, absent from the original API target. This image is reused without provider
logins or GitHub/DB credentials, with the existing `--network=none` checks profile and
fixed `python -m pytest -q` command. Acceptance must prove the effective profile.
Offline checks have no PostgreSQL URL and skip the disposable-DB concurrency tests;
the existing GitHub CI runs those as required checks before READY_TO_MERGE.

`ENABLE_AUTONOMOUS_CODE_WRITE` is still hard-coded false in compose. After host acceptance,
an explicitly approved compose override may set it true for one attended pilot; changing
`.env` alone cannot enable it. Guardian remains `enabled: false`.

Every worker tick tracks already-published Stage C tasks, including when writes are OFF.
It calls `stage_c_ci.ingest` (same PR, at most two CI repairs, normal daily/retry gates),
records only a verified founder merge and calls `stage_c_verify.step`. The existing host
watchdog now collects only the API/worker provenance variables (never a whole environment)
into `control_state.host_metrics.runtime`. MERGED waits for green main CI and merge ancestry
of **both** observed live SHAs, a host reading no older than five minutes and agreement with
the running worker's own SHA. Deploy the updated host `scripts/watchdog.sh` as well as the
images; an older collector with no SHAs means wait. No auto-merge or auto-deploy.
VERIFYING reuses the existing metric window; NOT_MEASURABLE is explicitly marked, never
represented as a measured improvement. This wiring creates no Learning or notebook entry.

Host inspection at 2026-10-05 20:52 UTC found no coder image/egress, an inactive launcher,
and no jobs directory. Codex workspace-write under no-new-privileges and both real provider
edits remain **unverified**. The local git/IPC-boundary fixtures are not a real pilot.
API auth likewise remains in log mode with no caller tokens on the running containers;
complete the existing HANDOFF step-2 caller rollout and exercise callers before enforce.

Check readiness without a model call or any mutation (host facts from
`scripts/stage-c-host-facts.py`, judged in the worker by `app/stage_c_preflight.py`):

```bash
ssh <production-host> sudo /opt/company-os/scripts/companyctl stage-c preflight [--json]
```

Each prerequisite prints PASS, FAIL or UNKNOWN; only PASS counts, exit 1 unless READY. The
code-write flag is shown but is the last switch, not a prerequisite. Use the `/opt/company-os`
copy: `/usr/local/bin/companyctl` is a separate, older install.

**Live gate (fail closed).** Acceptance is a past attestation, so worker admission also needs the live
half: the host watchdog runs this same preflight every tick (flag on or off) and stores it as
`control_state.stage-c-runtime-readiness`. A preflight that could not run is stored as NOT READY, so the
last READY never outlives the ability to check. `stage_c_worker.ready()` refuses a code write unless the
report is written by the watchdog, `ready: true`, under 15 minutes old (not future-dated) and for this
worker's SHA (`stage_c_runtime_not_ready`, zero model calls). A launcher, proxy or image that breaks after
acceptance stops the next claim; the alert is only the notification. After a deploy, expect up to one
watchdog interval (10 minutes) before the new SHA has a report.

Next founder actions, after green CI and review: approve the scoped deployment/install;
perform §4 steps 3–4 using separate coder subscription logins; record real acceptance;
then approve and run one test-only pilot. The founder merges its PR only after CI passes.
Main CI, live verification and an actual metric reading must follow before claiming the
pilot measured an improvement or enabling Guardian. No production change or pilot was
performed while preparing this PR.

Prepared pilot (not created or armed): change only `tests/test_metrics.py` to add a
future-skew boundary regression: a reading at NOW + five minutes is accepted, a reading
one microsecond beyond is rejected. Keep every existing assertion. Criteria: exact one-file
diff, the new test and full suite pass, independent provider approves its fingerprint.
The baseline moves as tests are added (seven when first written, 9 `test_*` functions in
`tests/test_metrics.py` on 2026-10-07); always remeasure on the pinned source immediately before arming. Record that baseline with the existing metrics API under the project's actual
business unit. Set `context.improvement.measure` to:

```json
{"selector": {"subject": "repository:Seerxo/os",
              "metric": "metrics_test_cases", "source": "pytest-collection", "window": ""},
 "direction": "up", "window_hours": 0}
```

After main CI and live verification, collect the tests from the deployed/pinned source
and record a fresh actual reading (baseline + 1, never invent it). An increased count
is a narrow regression-coverage metric, not revenue or proof of broader product quality.
Until the entire real sandbox → PR → founder merge → live → measured chain has evidence,
the pilot is incomplete and Guardian stays OFF.

## 5A. Standing authorization and the research hand-off

The one-task authorization stays the strictest form. For repeated safe work the founder may instead issue **one
standing authorization** (`POST /stage-c/standing`, verified founder principal only, never the host root):
named files that are already in `SAFE_FILES` (1-20), 1-3 runs per UTC day, 1-7 days, bound to the deployed
SHA. A deploy therefore voids it and a new release needs a new decision. Each claim spends one run under a row
lock and is audited (`stage-c.standing.issue/consume/revoke`). The worker tries the one-task authorization first.
Revoke with `POST /stage-c/standing/revoke` (founder or host operator). It enables nothing by itself:
`ENABLE_AUTONOMOUS_CODE_WRITE`, host acceptance, runtime readiness, admission, independent review, CI, the
founder's merge and the deploy all still apply.

A `code_change` candidate from an approved research run now becomes the Developer's task, with its origin
(`context.handoff`). Files are scoped only when the text names allow-listed files and **nothing else that looks
like a file**; otherwise the task stays an unscoped BACKLOG proposal. It is READY and autonomous only when a valid
standing authorization covers every named file and Stage C admission would accept it (ACTIVE project, approved
repository); otherwise it waits in BACKLOG. Side-effect wording still routes to approval. A proposed task cannot
propose further tasks, with one terminal exception: an auto-READY research task (depth 1) may hand `code_change`
tasks to the Developer (depth 2); every other kind is dropped, and depth-2 or Developer tasks create nothing. With the code-write flag off a READY write
task is skipped (`code_write_disabled`) with zero model calls.

## 6. What it would take for Codex to code (not approved; Codex reviews only)

Status 2026-10-07: Claude codes, another provider reviews. `app/stage_c_worker.py` holds the table
(`CAN_CODE = ("claude",)`, `CAN_REVIEW = ("claude", "codex")`). The founder declined `--sandbox
danger-full-access` and any loosening of the container or host.

**Measured on <production-host> (`codex sandbox` inside the launcher's coder profile, no model call):**

| Coder container profile | Result |
|---|---|
| as shipped (default seccomp and AppArmor, `cap-drop=ALL`, no-new-privileges) | `bwrap: No permissions to create a new namespace` |
| + `seccomp=unconfined` | `bwrap: Failed to make / slave: Permission denied` |
| + `seccomp=unconfined` + `apparmor=unconfined` | `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` |

Codex's inner sandbox (bubblewrap) needs user namespaces and more; the host also sets
`kernel.apparmor_restrict_unprivileged_userns=1`. Getting it to start means removing the outer
container's seccomp and AppArmor and adding capabilities, which trades the boundary we rely on
for one we would then have to trust Codex to enforce. `use_legacy_landlock` does not change the
backend in codex-cli 0.159.2.

**Routes worth investigating, none adopted.** A custom seccomp profile and a narrow custom AppArmor
profile that allow only the namespace operations needed; a newer Codex that supports hardened
containers without namespace privileges; a stronger outer sandbox (gVisor `runsc`, or a microVM if the
host supports KVM, which is unverified). Claims about vendor documentation and open issues that came
with this idea were not checked against their sources; read them before relying on them.

**Acceptance contract.** Codex becomes a coder only if the same boundary tests pass for the new
profile. "`codex exec` exited 0" is not acceptance; the escape attempts must fail. Items marked
(have) are already proven by `scripts/stage-c-host-wizard.sh accept`; the rest need new checks added
before the table changes.

| Check | Required result |
|---|---|
| edit inside `/workspace` | works (have, for Claude) |
| write outside `/workspace` | blocked |
| read host paths (`/etc/company-coder`, `/opt/company-os`, `/srv`, other jobs) | blocked |
| Docker socket | absent |
| host processes (PID namespace) | not visible |
| capabilities in the coder process | empty (verify from inside) |
| root filesystem | read-only |
| direct network, GitHub, arbitrary hosts | blocked (have) |
| provider hosts only | reachable (have) |
| credentials in the container | its own login only; no control or other-provider credential (have) |
| offline checks | pass (have) |
| independent reviewer on the produced diff | passes with the other provider (have) |

Then, and only then: the wizard gains a `codex_workspace_write_edit` check, the acceptance list
requires it again, and `CAN_CODE` gains `"codex"`. That is a security activation, reviewed and
merged by the founder, not a worker change. The check is also not retroactive: a Codex login,
launcher profile, image or Codex version change needs the contract re-run.
