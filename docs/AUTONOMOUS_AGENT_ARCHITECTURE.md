# AUTONOMOUS AGENT ARCHITECTURE

## Current status — source + host facts, 2026-10-07

Stage B is live. Stage C source wiring is deployed but remains disabled: Claude is the only
coder, Codex is an independent reviewer, and `companyctl stage-c preflight` must be READY with
code writes OFF before the one-file pilot. Deterministic dependency and configured external-event
release are implemented. This document's milestone tables below are historical design context;
live PostgreSQL and deployed SHA win for operational state.

Date 2026-10-01. Founder directive: "Autonomous Multi-Agent Company OS". Amends
`AGENT_EXECUTION.md` §3, which kept every model run attended on the laptop. That rule
now has a narrow exception: an **unattended worker on `<production-host>`**, staged so that
it starts with work that cannot change anything.

---

## 1. Current state

Already built and kept unchanged (see `AGENT_EXECUTION.md`, `GAP_ANALYSIS.md`):

| Piece | Where |
|---|---|
| PostgreSQL state: tasks, runs, events, incidents, approvals, decisions, learnings, notes, audit | `app/models.py`, Alembic |
| Four-agent registry with permissions, budgets, context and memory policy | `agents/registry.yaml`, `app/registry.py` |
| Deterministic outer-loop gate (`skip_reason`) and `companyctl run` | `app/dispatch.py` |
| Context Builder (minimum packet per task) | `app/context_builder.py` |
| Notebook memory and weekly compaction | `app/notebook.py` |
| Deterministic watchdog, heartbeats, Telegram with dedupe | `app/watchdog.py`, `app/notify.py` |
| Global pause (agents only, never production) | `app/control.py` |
| Attended inner loop: herdr control center (worktrees, reviewers, fingerprint approval) | laptop |
| Founder Desk (web, behind Cloudflare Access) | `app/static/desk.html` |

## 2. Gap analysis

| Directive asks for | Before this change | Now |
|---|---|---|
| Provider-agnostic agents (§3–4) | Models reached only through herdr on the laptop | `app/providers.py`: one interface, Claude Code + Codex adapters |
| Runs without the founder present (§24, §86) | Not possible: herdr needs the laptop | `company-worker` container, `restart: unless-stopped` |
| No paid fallback, cost circuit breaker (§8–9) | Policy only | Enforced in code: metered credentials → `METERED_BLOCKED`, SEV1 incident |
| Atomic task claiming (§25) | n/a | Conditional `UPDATE … WHERE status='READY'` |
| Structured result + validation (§30) | herdr `state.json` only | `AgentRunResult` (pydantic), JSON schema passed to both CLIs |
| Supervisor (§18) | herdr reviewer verdict | Deterministic verifier for worker runs; herdr path unchanged |
| Retries, backoff, loop detection, timeouts (§37–39) | herdr-side only | Worker: 3 attempts, 1/5/30 min backoff, identical-error loop flag, process-group kill |
| Crash recovery (§88) | n/a | Stale `STARTED` runs failed and requeued once |
| Cross-provider review (§19) | herdr `high-risk` workflow | Deferred: worker runs are LOW risk only (§7) |
| Telegram commands (§57) | Alerts only | Deferred (milestone W5) |

Not built, deliberately: a queue product, a scheduler product, a second orchestration
runtime. PostgreSQL plus a polling container covers them (§26, §104).

## 3. Target architecture

```
Founder Desk / companyctl / (Telegram later)
        │ creates tasks, pauses, approves
        ▼
Company OS API ── PostgreSQL ── events ── watchdog (cron, deterministic)
        ▲                          │
        │ reads/writes state       │
company-worker (container, polls every 60 s)
   tick → heartbeat → paused? → recover stale → SQL work-check
        │ no work: sleep, zero model calls
        ▼ opted-in READY task
   provider order from registry → health (zero-cost only) → atomic claim
   → Context Builder → envelope → provider.run → AgentRunResult
   → deterministic supervisor → task REVIEW / retry / FAILED / BLOCKED → events, notes
```

The attended herdr path stays for code work that changes repositories. Both write to
the same tables, so the Desk shows one company.

## 4. Provider design

`app/providers.py` is the only file that knows CLI syntax.

```python
provider.health_check() -> (Health, reason)    # no model call
provider.run(prompt, schema, cwd=, timeout_s=, max_turns=) -> ProviderResult
```

**Where the CLIs run (2026-10-06).** Not in `company-worker`. With `REASONER_URL` set,
`default_providers()` returns `RemoteProvider`s that POST one bounded JSON request to
`company-reasoner` (`app/reasoner.py`), which runs the same local provider classes and
returns the `ProviderResult`. The reasoner holds only the provider logins (the
`company-worker-codex/claude` volumes, `CLAUDE_CODE_OAUTH_TOKEN`); it is not on the
database network and has no `DATABASE_URL`, Telegram or GitHub token. `child_env`
filtering stays as defence in depth, but the container is the boundary, as for Stage C.
Unreachable reasoner = `BROKEN` = capacity: the worker falls back or waits.
Codex login now: `docker exec -it company-reasoner codex login --device-auth`.

| | Claude Code | Codex |
|---|---|---|
| Command | `claude -p … --output-format json --json-schema … --tools "" --permission-mode dontAsk --max-turns N --no-session-persistence` | `codex exec --sandbox read-only --ephemeral --skip-git-repo-check --json --output-schema … -o last.json` |
| Result | `structured_output` | last-message file |
| Zero-cost auth | `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) or keychain | `auth.json` with `auth_mode = chatgpt` |
| Metered → refuse | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, Bedrock/Vertex flags | `OPENAI_API_KEY`, `CODEX_API_KEY`, `auth_mode = apikey` |

Flags were taken from the installed CLIs (`claude 2.1.286`, `codex-cli 0.159.2`), not
from memory; the image pins those versions. `--dangerously-*` flags are never used.

Health: `AVAILABLE · AUTH_REQUIRED · RATE_LIMITED · QUOTA_EXHAUSTED · BROKEN · DISABLED ·
METERED_BLOCKED`. Error text is classified deterministically; "usage limit … purchase
more credits" is `QUOTA_EXHAUSTED`, and the answer is the next zero-cost provider or a
wait, never a purchase.

Routing is configuration: `providers:` in `agents/registry.yaml` (default
`[claude, codex]`, developer `[codex, claude]`). A provider that reports quota, rate or
auth trouble cools down for 30 minutes, so an exhausted subscription costs one probe per
window, not one per tick.

`session limit` is also quota exhaustion (verbatim production-message regression).
Daily `max_runs_per_day` counts work runs, not completed capacity-only attempts.
`tool_calls.capacity_failure=true` labels new no-capacity runs; older runs with only
fallback-health attempts and no supervisor verdict are likewise excluded. Unknown,
STARTED and supervised/real failed runs remain charged. This is a work-run budget,
not a claim of exact inference/token accounting; provider attempts remain auditable
and separately bounded by the existing cooldown. No schema migration is required.

The child process receives an allow-listed environment only (PATH, HOME, USER, locale,
subscription token, config dirs). Metered keys are both refused and stripped.

## 5. Task lifecycle (worker path)

```
READY (context.autonomous = true, LOW risk, no approval)
  → claim → IN_PROGRESS
  → APPROVED            → REVIEW   (never DONE: the founder closes tasks, §45)
  → REVISION / FAILED   → READY with retry_count+1 and backoff, or FAILED at max_attempts
  → ESCALATE            → BLOCKED
  → no provider capacity→ READY, no retry spent, one WORKER_UNHEALTHY alert
```

Every run writes `agent_runs` (provider, model, tokens, `estimated_cost_usd = 0`, list
price kept as `tool_calls.list_cost_usd`, attempts, prompt version), events, and an
audit row. Learning candidates become notebook notes; new-task candidates are stored on
the run for the CEO, never auto-created.

## 6. Trigger lifecycle

| Trigger | Mechanism |
|---|---|
| Schedule | worker poll (60 s) + existing cron (watchdog, backups, compaction) |
| Manual | Desk command bar / Create Task, `companyctl worker once|selftest` |
| Event / incident | watchdog emits events and opens incidents; a task with `autonomous: true` is what the worker acts on |
| Dependency | `skip_reason` refuses work whose dependencies are not DONE |

No LLM decides whether time has passed or work exists.

## 7. Autonomy rollout

| Stage | Scope | Status |
|---|---|---|
| A | observe + report | done (watchdog, brief, Desk) |
| **B** | **automatic low-risk tasks, no tools, empty scratch dir** | **this change** |
| C | reversible engineering in an isolated worktree, `workspace-write`, per-repo lock | next |
| D | selected production workflows behind approval gates | later |

Stage B gate, enforced in `_eligible`: `context.autonomous == true`, `risk_level = LOW`,
`approval_required = false`, agent enabled, business unit active, dependencies done.
Existing tasks are not opted in, so turning the worker on changes nothing about them.

## 8. Memory lifecycle

Unchanged: run → notes (`notebook.record`) → weekly compaction → candidate learning →
supervisor/founder. The worker never writes `learnings` or the constitution directly.

## 9. Approval lifecycle

Worker runs never reach work that needs approval (Stage B gate). A result with
`status = needs_approval` is escalated to BLOCKED rather than acted on. Approvals keep
going through the existing Approval Center.

## 10. Failure lifecycle

| Failure | Handling |
|---|---|
| Malformed or wrong-task output | supervisor FAILED; never touches state beyond the run row |
| Timeout | process group killed; failed run |
| Worker crash / restart | run `STARTED` past timeout + 5 min → FAILED, task requeued once |
| Same error at every attempt | `TASK_FAILED_REPEATEDLY` flagged "loop detected", task FAILED |
| All providers out of capacity | task waits, providers cool down, one alert |
| Metered credentials | worker refuses, SEV1 incident, `INCIDENT_CREATED` alert |
| Agents paused | heartbeat only |

## 11. Implementation milestones

| M | Scope | Verification |
|---|---|---|
| W1 ✅ | Providers, worker loop, supervisor, retries, recovery, container, `companyctl worker` | 17 worker tests; real Claude run end to end; kill -9 recovery |
| W2 ✅ | Deploy `company-worker`; founder logs in both CLIs; server self-test | server self-test passed |
| W3 ✅ | Watchdog `check_worker` (stale heartbeat only, deduped); Desk worker + provider panel from `worker_state()` | tests: never-started is silent, 30 min stale alerts once; panel rendered from a real run. Live after W2 |
| W4 | Stage C: worktree + `workspace-write` + repo lock; cross-provider review for code | golden tasks per agent |
| W5 | Telegram `/status /brief /tasks /approvals /pause /resume` + message → task | |
| W6 | Scheduled CEO review once TASK-0005 gives a revenue baseline | empty state → zero model calls |

## 12. Stage C security design (not enabled)

Goal: a READY developer task becomes a reviewed diff without the founder's laptop, and
without the model ever holding a credential that matters.

```
company-worker (control: DB creds, GitHub key, no model)
   │ repo lock (control_state "lock:repo:<name>", lease = run timeout)
   │ git worktree add  /workspaces/<TASK>  from a local mirror, branch task/<TASK>
   │ writes /workspaces/<TASK>/job.json
   ▼
company-coder (sandbox: model CLI only)
   │ claude: --tools Read,Edit,Write,Glob,Grep,Bash  --allowedTools "Bash(pytest *)" …
   │ codex : --sandbox workspace-write  -C /workspaces/<TASK>
   │ runs the project's own checks, writes result.json + diff.patch
   ▼
company-worker
   deterministic checks → independent review (other provider, read-only, diff only)
   → fingerprint = sha256(diff) → Approval row → founder approves that fingerprint
   → worker pushes the branch / opens the PR. Never merges, never deploys.
```

| Boundary | company-coder may | company-coder may not |
|---|---|---|
| Filesystem | `/workspaces/<TASK>` read-write; named project files read-only | host `/root`, `/opt/*`, other worktrees, `.env`, SSH keys, Docker socket |
| Credentials | its own CLI subscription login (volume) | `DATABASE_URL`, Telegram, GitHub key, Cloudflare, any production secret |
| Network | egress only through an allow-list proxy (open-source, on the host) | everything else; no inbound ports |
| Git | edit files in its worktree | fetch, push, merge, change remotes (no key exists inside) |
| Processes | non-root uid, `no-new-privileges`, cpu/mem/pids limits, per-task timeout | privileged mode, host PID/network namespace |

Egress allow-list, starting point: the provider endpoints the two CLIs call
(`api.anthropic.com`, `claude.ai` auth, `chatgpt.com` / OpenAI auth). No package
registries by default: dependencies come pre-installed in a per-project test image, so a
model cannot pull arbitrary code. Each added domain is a reviewed registry change.

Repository lock: one mutating task per repository, held in `control_state` with a lease
so a crashed run frees it through the same stale-run recovery that exists today.

Reuse from `herdr-project-control-center`: fingerprint approval, the bounded transition
limit (8), and risk-tiered review (`high-risk` → two reviewers). Concepts and, where
practical, code; not a second orchestration engine.

Enablement requires, in order: the deployment platform's control plane not reachable from the internet, founder approval
of this design, the coder container and proxy deployed, golden tasks passing (inspect
before edit, stays in scope, verifies; supervisor rejects unsupported "done"). Until then
`event_router` tasks keep `autonomous: false` and the worker stays Stage B.

### Founder decision 2026-10-05: the gate is the merge, not the push

Supersedes the "founder approves that fingerprint → worker pushes" step above. After the
project's checks pass and an independent review (other provider, read-only, diff only)
approves fingerprint F, the control plane (never the coder) pushes `task/<KEY>` and opens
the PR itself; the pushed branch head diff must equal F, otherwise nothing is pushed. The
existing GitHub CI runs; the Desk shows **Ready to merge**; the founder merges on GitHub.
Still forbidden: auto-merge, deploy, any write to `main`, force push. GitHub branch
protection is unverified on the current plan (REPOSITORY_GUARDIAN.md), so these limits are
enforced by Company OS itself: the only ref the control plane ever pushes is `task/*`.

Improvement lifecycle, recorded on the task (`context.improvement`) with an audit row per
step: DETECTED → PLANNED → IMPLEMENTING → TESTING → INDEPENDENT_REVIEW → PR_OPEN → CI →
READY_TO_MERGE → MERGED (founder only) → VERIFYING → MEASURED. Failed tests, review or CI
return to IMPLEMENTING (bounded repairs); unmerged work may become ABANDONED.

### Founder decisions 2026-10-05: recording the merge, verifying and measuring

**MERGED is recorded from GitHub evidence, never declared by automation.** `stage_c_ci`
records it only when GitHub's `merged_by` is the founder's login, the PR is not a draft,
it merged into the tracked branch from READY_TO_MERGE, and its head is the published commit.
The actor reads `founder (github:<login>, via stage-c-ci)`, and `advance` refuses that label
without `merged_by` and `merge_commit` evidence. Any other merge (another login, a bot, a
merge before CI passed) is never recorded and blocks the task for the founder.

**Verify and measure are deterministic** (`app/stage_c_verify.py`, no model call).
MERGED → VERIFYING when main CI on the merge commit succeeded and the live SHA contains it.
A red main blocks the task. VERIFYING → MEASURED from a metric the task names
(`improvement.measure`): the first reading after the window against the baseline taken at
verification. With no metric named, the outcome is NOT_MEASURABLE, shown to the founder.

Built (`app/stage_c.py`, not wired, flag stays OFF): repository lease with a fencing token
in `control_state` (an expired holder's token stops passing `holds()`, checked under the row
lock right before a push), per-task worktree on `task/<KEY>` from a local mirror with a
validated key, diff fingerprint, lifecycle transitions. `app/stage_c_runner.py` (not wired)
connects them for one attempt: lease → scoped export → `coder(workspace)` → `import_changes` →
worktree at base + diff → `checks(copy)` → INDEPENDENT_REVIEW with fingerprint F, or back
to IMPLEMENTING. Coder and checks receive a directory only; checks execute model-written code,
so they run in the network-less sandbox, never inside company-worker. `review()` then has
the other provider judge exactly F: it receives a data packet only (objective, criteria, F,
check summary, diff), an approval counts only if it names F, self-review and untested bytes
are refused, and a missing or invalid verdict is neither approval nor rejection. A Stage B
provider (no tools, empty scratch) is the reviewer via `provider_reviewer`. `publish()` then
pushes `task/<KEY>` and opens the PR only if the commit's tree equals base + the approved diff
(so nothing unreviewed rides along), from base, under a re-acquired lease, without force,
over https only, and records PR_OPEN. A failed PR keeps the branch and does not claim PR_OPEN.
The push credential belongs to the control plane, scoped to one repository (founder
approved 2026-10-05). Golden
scenarios: `evals/test_stage_c_runner_evals.py`. Still required before enablement:
coder container + egress proxy (separate approval), a GitHub credential scoped to one
repository for the control plane only, golden tasks, then wiring into the worker.

## Verified 2026-10-01

- **Real Claude run, no fakes:** self-test task created → research agent → `claude -p`
  (sonnet) → structured result → supervisor APPROVED → REVIEW, 9.3 s, events and audit
  written, second tick made zero model calls.
- **Real fallback:** with Claude unauthenticated and Codex out of quota, both were
  classified (`AUTH_REQUIRED`, `QUOTA_EXHAUSTED`), nothing was purchased, no retry spent.
- **Real crash:** worker `kill -9` mid-run → task held, no double run → stale run
  recovered once → backoff respected → single successful re-run.
- **Image:** builds on `<production-host>`, runs as non-root, CLIs at pinned versions;
  `ANTHROPIC_API_KEY` in the environment makes every provider `METERED_BLOCKED`.
