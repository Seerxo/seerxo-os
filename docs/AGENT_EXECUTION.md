# AGENT EXECUTION — Company OS and herdr

Date 2026-09-30. Supersedes the agent-runtime sections of `GAP_ANALYSIS.md` (§3, §4, §10, §12 milestones 6–8).

**The headline: most of the agent layer is already built, and it is not in this repository.**

`herdr-project-control-center` v0.4.0 — the founder's own herdr plugin — already implements
the inner-loop role/model router, supervisor, approval gates, bounded agent loops and
founder desk. Company OS adds only the outer business router: should this task run, and
which job owns it? Building a second execution runtime would violate §104.

This document defines the seam instead.

---

## 1. WHAT EXISTS, VERIFIED

### herdr (third party, v0.9.3, Homebrew)
Terminal workspace manager for AI coding agents. Relevant surfaces:

```
herdr --session <name>              named, resumable sessions
herdr --remote <ssh-target>         drive a remote machine
herdr --machine <label> <command>   multi-machine addressing
herdr api snapshot                  live session state, machine readable
herdr api schema                    bundled API schema
herdr server stop | reload-config   daemon control (socket at ~/.config/herdr/herdr.sock)
herdr plugin link <dir>             local plugin development
```

Config: `[session] resume_agents_on_restore = true` — agents survive a restart. `[worktrees] directory` — isolated worktrees per task. The sidebar is configured to show, per agent, `state_icon $role agent state_text` then `$task` then `workspace tab $summary`: it is already an agent status board.

### herdr-project-control-center (the founder's code, v0.4.0, ~70 KB JS)
```
router.toml     9 roles -> concrete agent configs; 4 risk-tiered workflows
adapters/       docker, generic, go, next, node, python, rust  (check commands per stack)
src/task.mjs    42 KB — task state machine
src/dashboard.mjs  14 KB — the Command Center popup
src/router.mjs · check-runner.mjs · metrics.mjs · project.mjs
bin/hctl        CLI
test/
```

What it does, in its own words: *"The controller owns worktree creation, role routing, task state, checks, review fingerprints, approval, and recovery. Worker agents never receive orchestration-enabled profiles."*

Mapped against the constitution:

| Constitution | Already implemented in the control center |
|---|---|
| §16 dispatcher / router | `router.toml` roles + workflows; `src/router.mjs` |
| §45 Supervisor, mandatory | `hctl task review`, structured review, agents cannot self-approve |
| §72 approval engine | `hctl task approve T42 <exact-fingerprint>` — approval requires the exact fingerprint, so it cannot be waved through |
| §71 loop detection | bounded automation: at most 8 state transitions, then pause; `--max-steps=1..20` |
| §84 model routing | `quick`→Luna low, `standard`→Terra medium, `high-risk`→Sol + independent Codex **and** Claude reviews, `research`→Kimi + read-only scout |
| §85 least privilege | worker agents never get orchestration-enabled profiles; reviewers run `--sandbox read-only --ephemeral` |
| §73/§75 production safety | `finish` does not commit, push, open a PR, merge, deploy, install dependencies, run migrations, delete data or remove git state |
| §87 founder desk | Command Center popup (`Option+D` / `prefix+d`), overview + agent + project + check + settings views |
| §121 agent performance | `hctl metrics` — JSON scorecard, "adds no service, database, or dependency" |
| §31/§35 task model | task packet at `.herdr/tasks/<ID>.md`, `--acceptance=` is the success criterion |
| §143 isolation | one `task/<id>-<slug>` worktree per task |

Zero-cost check (§26A): every role routes to a CLI the founder already has — `pi`, `codex`, `claude`, `kimi`, `glm`. No new paid service. Reviewers run ephemeral and read-only, which is also the cheapest way to review.

---

## 2. THE SEAM

The two systems answer different questions, and neither should answer the other's.

```
┌─────────────────────────────────────────────────────────────────┐
│ COMPANY OS   (<production-host>, unattended, deterministic)        │
│                                                                  │
│ WHY and WHAT:                                                    │
│   business units · projects · goals · priority                   │
│   services registry · incidents · events · decisions · learnings  │
│   backups + restore verification · watchdog · Telegram · audit    │
│                                                                  │
│ Owns the OUTER loop: trigger -> wake -> context -> checkpoint     │
└───────────────────────────┬─────────────────────────────────────┘
                            │  a task with success criteria
                            │  (and, coming back, a run record)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ HERDR + PROJECT CONTROL CENTER   (laptop, attended)              │
│                                                                  │
│ HOW:                                                             │
│   role routing · worktree · writer · checks · structured review   │
│   fingerprint approval · recovery · bounded loop                  │
│                                                                  │
│ Owns the INNER loop: task -> plan -> tool -> verify -> review     │
└─────────────────────────────────────────────────────────────────┘
```

This is exactly §36A's two-loop split, now with a concrete owner for each loop instead of one system pretending to be both.

### What Company OS therefore does NOT build

Cancelled from the plan, with the reason:

| Was planned | Cancelled because |
|---|---|
| Agent runtime / inner loop executor | `src/task.mjs` is a 42 KB state machine that already does this, with recovery |
| Inner-loop role/model router | `router.toml` + `router.mjs` |
| Supervisor agent | `hctl task review` + fingerprint approval, with independent reviewers on high risk |
| Model routing logic | `router.toml`, already risk-tiered and cost-tuned |
| Per-agent prompt versioning | the control center owns agent invocation |
| A second founder desk UI | Command Center exists; `companyctl brief`/`desk` covers the server side |

That removes milestones 6, 7 and most of 8 from `GAP_ANALYSIS.md` §12. The honest saving is weeks of work that would have been worse than what exists.

### What Company OS keeps building

Nothing in the control center knows about revenue, business units, incidents, backups or the server. Those stay here:

- **Business priority.** The control center executes a task; it has no opinion on whether the Seerxo revenue baseline matters more than a Cryonel refactor. §4 and §103 live in Company OS.
- **Server-side watchdog.** Runs unattended on `<production-host>` every 10 minutes. The laptop may be closed.
- **Backups and restore verification.** Server-side, and the control center explicitly adds "no service, database, or dependency".
- **Institutional memory.** Decisions, learnings, incidents, checkpoints — the things that must outlive both a session and a laptop.
- **Alerting.** Telegram with allowlist and dedupe.
- **Audit trail** across business actions, not just code tasks.

---

## 3. THE TENSION THIS DESIGN DOES NOT HIDE

> **Amended 2026-10-01** by `AUTONOMOUS_AGENT_ARCHITECTURE.md`: an unattended
> `company-worker` on the server now runs opted-in, LOW-risk, tool-less tasks with the
> founder's existing subscriptions. Code work that changes repositories stays here, on
> the attended herdr path, until Stage C.

**herdr runs on the laptop. Company OS runs on the server.**

An agent layer that needs the founder's laptop awake is not autonomous. That is a real limit, not a detail, and it decides what each side may be trusted with.

`herdr --remote <ssh-target>` and `herdr --machine` exist, and `herdr server` is a daemon, so running it against or on `<production-host>` is technically possible. Two reasons not to do it yet:

1. **Credentials.** Every role in `router.toml` routes to a CLI authenticated on the laptop (`pi`, `codex`, `claude`, `kimi`, `glm`). Running them headless on the server means putting those credentials on a shared production host. That is a security decision and a provider-terms question — the founder's call, not mine.
2. **Unproven.** `hctl doctor` currently reports `server_binary_stale: yes` and three integrations needing update on the laptop, where herdr is well exercised. Server-side headless operation is not something to discover during an incident.

### Recommendation

**Attended cockpit + unattended spine.**

- **herdr = the founder's cockpit.** Interactive, laptop-side, for code work: TASK-0002 (YouTube repair), TASK-0006 (git), TASK-0008 (MCP documents). High-quality review and approval gates, with the founder present.
- **Company OS = the unattended spine.** Watchdog, backups, alerting, memory, priority. Runs whether or not anyone is awake.

Autonomy stays at **Level 2** (§113) for code work: the agent executes, the founder approves via fingerprint. Level 3 remains where it already legitimately is — `youtube-auto` publishing, a founder-accepted exception.

Server-side herdr would put model credentials on the production host; that stays a separate security decision.

---

## 4. INTEGRATION — SMALLEST USEFUL VERSION

Three thin pieces. No new service, no new dependency, no daemon.

### 4.1 Hand a task down — ✅ BUILT 2026-09-30

```bash
ssh <production-host> sudo companyctl dispatch TASK-0006   # the plan, as JSON (server)
company-dispatch TASK-0006                                # plan + command  (laptop)
company-dispatch TASK-0006 --run                          # actually start it
ssh <production-host> sudo companyctl context TASK-0006     # minimal context packet
```

`companyctl run` performs the zero-cost outer-loop pass first: assigned agent enabled,
business unit active, dependencies done. It records the check and returns only runnable
tasks plus their exact `company-dispatch ... --run` commands. It does not call a model.

The mapping rules live in `app/dispatch.py` — server-side, pure and tested (31 cases) — so there is one place to change them. `hctl` runs on the laptop, so the server's job ends at producing an unambiguous instruction; `company-dispatch` resolves the local checkout and executes.

The plan also carries a `context_command`. `company-dispatch` records that exact command
as an additional acceptance item in the herdr task manifest, so the worker loads context
on demand from Company OS rather than receiving a repository dump. The context endpoint
applies the assigned agent's registry policy and returns only related project/business
state, decisions, learnings, the agent's notes, recent related runs, declared files and
required tool state.

Fields map as follows:

```
Company OS task          ->  hctl task start
─────────────────────────────────────────────────────
key            TASK-0006 ->  <id>            T6
title                    ->  "<title>"
success_criteria         ->  --acceptance="..."
context_command          ->  --acceptance="Load task context: ..."
project.repository       ->  <repo path>
context.scope            ->  --scope=...
risk_level LOW           ->  workflow quick
risk_level MEDIUM        ->  workflow standard
risk_level HIGH/CRITICAL ->  workflow high-risk   (Sol + Codex + Claude review)
approval_required        ->  never --auto; stop at fingerprint approval
```

The risk mapping matters: Company OS's §34 risk level *chooses the reviewer strength*, automatically. A HIGH task gets two independent reviewers without anyone remembering to ask.

Three behaviours worth stating, all verified against live tasks:

- **Automation is suppressed independently of cost.** `approval_required` or HIGH/CRITICAL risk removes `--auto`, while the workflow is still chosen on risk alone. A cheap task can still be gated; an expensive one is never ungated.
- **Non-workable states are refused, not silently started.** `TASK-0001` is `WAITING_EXTERNAL` (waiting on R2 credentials) and dispatch returns 409 with the reason. Starting work that cannot finish wastes a model run and muddies the task history.
- **The hctl id is the Company OS key verbatim.** The worktree becomes `task/TASK-0006-<slug>`, so the reporter (§4.2) matches runs back with no lookup table.

Repository caveats are surfaced rather than dropped: the registry records youtube-auto as *"semihbugrasezer/youtube-auto (server copy at /opt/youtube-auto is NOT a git repo — drifted)"*, and the plan prints that note while still resolving the bare name.

### 4.2 Bring the result back — ✅ BUILT 2026-09-30

```bash
company-report TASK-0006                 # what would be recorded (laptop)
company-report TASK-0006 --send          # record it
company-report TASK-0006 --send --learning "migrations run before restart"
```

Reads the control center's own state at
`~/.local/state/herdr/plugins/project-control-center/tasks/<ID>/state.json` and posts it
through **unchanged**. Company OS interprets it (`app/runreport.py`, 17 tests) rather
than trusting a summary written about it.

`company-report` can still preview every run. Recording a report that moves a task
to REVIEW requires a verified founder Access session at
`POST /tasks/<KEY>/run-report`. The local/host operator and admin token can still
record non-REVIEW outcomes on open tasks. The API treats the posted `state.json` as
caller-supplied data, so an operator report alone cannot authorize REVIEW.

**The control center's review is the supervisor of §45, so its verdict is translated,
never second-guessed.** Forming an independent opinion here would recreate the
self-approval problem one layer up.

| herdr state | run | supervisor | Company OS task |
|---|---|---|---|
| `finished` + `PASS` | SUCCEEDED | APPROVED | **REVIEW** |
| `finished`, checks passed, no reviewer (`quick`) | SUCCEEDED | APPROVED | REVIEW |
| `finished`, nothing recorded | SUCCEEDED | *none* | REVIEW |
| review `REQUEST_CHANGES` | SUCCEEDED | REVISION_REQUIRED | IN_PROGRESS |
| checks `failed` | FAILED | FAILED | IN_PROGRESS, retry_count+1 |
| `blocked` or verdict `BLOCKED` | ESCALATED | ESCALATE | BLOCKED |
| `cancelled` | FAILED | *none* | READY — cancelling is not failing |
| anything else | STARTED | *none* | IN_PROGRESS |

Two rules the tests pin down:

- **The mapping can never produce `DONE`.** The control center approves *code*; only the
  founder closes a *task*. Eight different states are asserted against this.
- **`blocked` outranks a passing check.** A run that passed its gate and then blocked is
  still blocked; the strongest signal wins rather than the most recent one.

Changed files are read from git against `source_commit`, not from the agent's account of
itself. `TASK_COMPLETED` is deliberately **not** in `ALERTABLE_EVENTS`: §111 forbids
paging the founder for routine success, and this was verified — a successful run records
everything and sends nothing.

### 4.3 `agents/registry.yaml` — one registry, two consumers
Company OS's `agents` table and herdr's `router.toml` describe overlapping things with different vocabularies. Rather than sync them, the registry maps them:

```yaml
developer:
  role: Software / Infrastructure
  owns: [repositories, deployments, technical_incidents]
  execution: herdr
  model_class: REASONING
  work_check: assigned_ready_tasks
  herdr:
    workflow_by_risk: {LOW: quick, MEDIUM: standard, HIGH: high-risk, CRITICAL: high-risk}
```

`router.toml` stays the source of truth for *which model*. The registry is the source of truth for *which business responsibility*. Neither duplicates the other.

---

## 5. REVISED MILESTONES

| M | Was | Now |
|---|---|---|
| 1 | Foundation | ✅ done |
| 2 | Watchdog | ✅ done |
| 3 | Tests | ✅ done |
| **4** | ✅ Agent registry | **Done 2026-09-30.** Exactly four Foundation agents with full contracts, identities, versioned prompts and shared skills. See §7 |
| **5** | ✅ Notebook memory | **Done 2026-09-30.** `agent_notes` + deterministic weekly compaction. See §8 |
| **6** | Outer router + hand-off | ✅ **done** — `companyctl run`, `companyctl dispatch` and `company-dispatch`, §4.1 |
| **7** | Minimum context packet | ✅ **done** — `companyctl context` and policy-scoped herdr hand-off, §4.1 |
| **7** | ~~Agent runtime + Supervisor~~ | ✅ **done** — `company-report` + `app/runreport.py`, §4.2 |
| **8** | Developer agent | unchanged, and now much cheaper: run TASK-0006 through `hctl task start --auto` and report the result back |
| 9 | Metrics + scheduled CEO | CEO is available manually; automatic prioritisation waits for TASK-0005 |
| 10 | Telegram commands | unchanged |
| 11 | Dashboard | **lower priority** — Command Center plus `companyctl desk` covers the founder's needs |

---

## 7. THE ROSTER AND THE DESK — BUILT 2026-09-30

### Four agents defined and enabled

Foundation follows §105 literally: CEO, Developer, Supervisor and Research are the only
agent definitions. The watchdog is deterministic code in `app/watchdog.py`, not a model
with a job title. Product, Growth, Finance, YouTube, Publishing, QA, Compliance and other
specialists remain in the plan until real work needs one.

All four definitions are enabled, but **enabled means eligible, not invoked**. Each has a
deterministic `work_check`; none has a cron schedule in Foundation. The CEO remains
available for founder-requested briefs while automatic prioritisation waits for TASK-0005.
All model budget fields are zero with `existing_access_only: true`.

Identity lives in git; runtime state (`enabled`, heartbeat, last error) lives in the
`agents` table. `companyctl registry-sync` reconciles one into the other and **refuses
on validation problems** — a roster that cannot pass its own checks should not become
runtime state. Rows absent from the file are retained for audit but disabled; they are
never deleted (§23) and cannot remain runnable after removal from Git.

Validation enforces, rather than trusts: every agent has triggers, capabilities, allowed
tools, a deterministic work-check and resolvable shared skills; `autonomy_level` may not
exceed 3, and business units must exist.

**One bug this caught before it mattered.** Policy overrides originally *replaced* the
defaults, so `supervisor` declaring its own `forbidden: [approve_own_work]` silently
dropped `drop_database`, `send_money` and `rewrite_constitution` from its own floor — a
registry edit could have granted itself powers the constitution denies. Permission lists
now union with the defaults; the safety floor may only grow.

### The desk

```bash
ssh -N -L 8099:127.0.0.1:8099 <production-host>    # then open http://localhost:8099
```

One HTML file served by the same API: priorities, incidents, approvals with working
grant/reject buttons, the agent roster with budgets, backup state and recent activity.

It binds **`127.0.0.1:8099` only**. That is not public exposure — nothing outside the
host can reach loopback, `ufw` is untouched, and §110's question is still answered no.
Verified: direct access from outside returns `000`, through the tunnel `200`. The page
needs no login of its own because anyone who can open the tunnel already has shell
access to the host.

Served as one self-contained file with no CDN: a dashboard that needs its own deployment
pipeline is a second system to keep alive (§104). Since 2026-10-05 (founder request) the
file is built from `desk/` (React + shadcn/ui) on the laptop; servers still only serve the
committed `app/static/desk.html`, and CI checks it matches the source.

**A second bug the desk itself exposed.** Its "recent activity" panel showed three
`WORKER_UNHEALTHY` alerts, all delivered. Syncing the roster had enabled four agents, and
the watchdog alerted because they had no heartbeat — but enabling a *definition* does not
start a *process*. The code contradicted its own docstring, which already said a
never-started agent is not a fault. A missing heartbeat is now ignored; only a stale one
alerts. Three Telegram messages went out before the fix.


---

## 8. NOTEBOOK MEMORY — BUILT 2026-09-30

The founder's "defter", made controlled. §53A was a rule with no implementation; it now
has one, and the rule it enforces is that **a note is not a learning**.

```
run ends        -> notes            cheap, may be wrong, merged on sight
weekly review   -> deduplicate      deterministic: normalise, hash, count
                -> promote          recurred >= 2 times -> CANDIDATE learning
                -> archive          single reference, unseen for 30 days
                -> never delete     archived_at + reason (§155)
```

### What changed in the return path
`company-report --learning` used to write straight to `learnings`. It now writes a
**note**. Writing directly to learnings skipped the review the rule exists for: a lesson
one run happened to draw is an anecdote until a second run draws it too.

### Deduplication is deterministic (§36A)
No model is asked whether two notes mean the same thing. Notes are normalised —
lowercased, stopwords dropped, short tokens dropped, remaining words sorted — then
hashed. So these merge into one note with `reference_count = 2`:

```
"Run the database migrations before restarting the service"
"Before restarting the service, run database migrations"
```

Merging is what makes the count meaningful: **two independent runs** learning the same
thing is a pattern; the same run saying it twice is not.

### Promotion proposes, it does not decide
A promoted note becomes a learning titled `Candidate rule (N runs): …`, with evidence
naming how many runs and over what period. §53A still routes it through the supervisor,
and the founder for high impact, before it becomes permanent. **No agent writes to
`CLAUDE.md`.**

Verified end to end: two phrasings merged to one note, a dry run changed nothing, apply
promoted exactly one, and a second apply promoted **zero** — idempotent, no duplicate
candidates.

### One guard worth naming
If the candidate learning came back without an id, the note would keep
`promoted_learning_id = NULL` and be promoted again at every weekly review, multiplying
candidates silently. That now raises instead. The failure was only visible because the
test double did not assign primary keys the way a real session does — which is the useful
kind of test-double bug, since it pointed at a genuine production failure mode.

Scheduled `0 5 * * 1` (Mondays, local time).
