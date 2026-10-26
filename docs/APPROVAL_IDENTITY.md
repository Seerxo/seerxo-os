# Approval identity and the founder authorization contract

Status: implemented in `app/approval_identity.py`, `app/api_auth.py`, `app/cloudflare_access.py`, `app/main.py`.

## Threat model

| # | Threat | Before | Now |
|---|---|---|---|
| T1 | A caller reaching `POST /approvals/{key}` writes `decided_by: "founder"` in the body | Stored verbatim as the decider and audit actor | Body is an untrusted hint. Identity comes from the verified principal; the claim is kept only as audit evidence (`client_claimed_decided_by`) |
| T2 | Admin, host, local, Desk-token or YouTube-token callers are treated as the founder | Any of them could decide | Only a verified Cloudflare Access assertion is a founder. Operators decide as `operator:local` / `operator:host`; admin, Desk and YouTube tokens and anonymous callers are refused |
| T3 | `API_AUTH_MODE=log` lets denied writes through | Approval and any new experiment route open in rollout mode | `api_auth.ENFORCE_ALWAYS` routes are enforced in every mode |
| T4 | Downstream code trusts `decided_by == "founder"` | A forged row passes | `verified_founder_decision()` requires GRANTED + `founder:access:` prefix + the endpoint-written `approval.decide` audit row for that actor |
| T5 | Revoked founder (email removed, JWKS unreachable) | n/a | Verification fails closed: 401, no approval, task or audit change |

## Identity sources

- `founder:access:<email>`: verified Access assertion (fixed issuer/JWKS, audience, `CF_ACCESS_DESK_EMAILS`). The only founder proof.
- `operator:local` / `operator:host`: host loopback or `API_HOST_IPS` network position. A person with root on the host, not necessarily the founder.
- `service:admin|desk|youtube`: possession of a bearer token. Never a person.
- `anonymous`.

A bearer token wins over an assertion on the API host (existing behaviour), so a service token plus an assertion is `service:*`, not founder.

## Contract for Agent 2 (Experiment Engine routes)

1. Founder-only steps (READY approval, accept/reject decision) must call `approval_identity.require_founder(request)` and record `principal.id`. Never build identity from a header, body field or `founder (via <caller>)`.
2. Other mutations record `principal_of(request).id` as the actor.
3. All `POST /experiments...` routes are already in `ENFORCE_ALWAYS`; keep new mutation routes inside those patterns or add them there.
4. The engine must not treat an operator or service principal as a decision-maker for accept/reject.

## Contract for Agent 4 (Stage C cleanup)

1. Cleanup authority = `verified_founder_decision(db, approval)` on the approval bound to the task and plan digest, in addition to the existing preview-digest, freshness and `TrustedBoundary` checks. Replace the `decided_by == "founder"` comparison.
2. The approval's `action` must still bind task key and digest; this module only proves who decided.
3. Do not expose cleanup through a route until the route itself calls `require_founder`.

## Founder-only gates (second hardening pass)

| Gate | Rule |
|---|---|
| `POST /tasks/{key}/status` to DONE/CANCELLED/FAILED/REVIEW | `require_founder(request)`; DONE still requires evidence or result (422); `body.actor` is untrusted |
| `POST /tasks/{key}/status` from existing DONE/CANCELLED/FAILED/REVIEW | `require_founder(request)` for reopen and protected result changes |
| `POST /tasks/{key}/run-report` to REVIEW, or from existing DONE/CANCELLED/FAILED/REVIEW | `require_founder(request)`; posted Herdr state is caller-supplied |
| `POST /phases/{n}` to COMPLETE | `require_founder(request)`; evidence still required |
| `POST /approvals/{key}` granting HIGH/CRITICAL risk | verified founder only; an operator may *reject* these and decide LOW/MEDIUM ones as itself |
| Other transitions on those routes | unchanged and still reachable by the same callers; the audit actor is the verified principal id and the client's `actor` is kept as `client_claimed_actor` |

The Access founder can reach the two routes through `api_auth.FOUNDER_SCOPE`; the legacy Desk bearer token cannot (an attached assertion is ignored when a bearer is present). In-process callers with no request carry no identity and fail closed for DONE/COMPLETE.

## Privileged route inventory (main e27b8df + this branch)

| Route | Class | Rule |
|---|---|---|
| `POST /tasks/{key}/status` -> DONE/CANCELLED/FAILED/REVIEW | Founder-only | `require_founder`; DONE requires evidence |
| `POST /tasks/{key}/status` from existing DONE/CANCELLED/FAILED/REVIEW | Founder-only | `require_founder`; includes same-status result changes |
| `POST /tasks/{key}/run-report` -> REVIEW or from existing DONE/CANCELLED/FAILED/REVIEW | Founder-only | `require_founder`; caller-supplied Herdr state is not independent proof |
| `POST /phases/{n}` -> COMPLETE | Founder-only | `require_founder` |
| `POST /approvals/{key}` granting HIGH/CRITICAL | Founder-only | verified founder; operators may reject, and decide LOW/MEDIUM as themselves |
| `POST /learnings/{id}/approve` | Founder-only, enforce-always | `require_founder`; locks the row; audit actor = `Principal.id` |
| `POST /stage-c/authorization` | Founder-only, enforce-always | `require_founder`; SHA is the deployed SHA (API and worker must agree); task, repository, action, TTL <= 60 min bound and audited |
| `POST /control/agents` resume | Founder-only | `require_founder`; pausing (emergency stop) stays open to every authorized caller and records the real principal |
| `POST /stage-c/authorization/revoke` | Founder or host operator, enforce-always | withdrawing authority is the safe direction; audit names who |
| `POST /experiments...` founder steps (approve, accept, reject) | Founder-only, enforce-always | route layer is Agent 3's; contract below |
| `POST /experiments...` other steps | Authenticated principal, enforce-always | actor = `principal_of(request).id` |
| `POST /learnings`, incident resolve (learning) | Authenticated operator | always writes a CANDIDATE; created_by/audit = real principal |
| `PATCH /goals/{id}`, `POST /tasks/{key}/assign` | Authenticated operator / Desk | unchanged authority; audit actor = real principal, client `actor` kept as `client_claimed_actor` |
| Checkpoints, notes, decisions, incidents, metrics, webhooks | Scoped service / operator | unchanged; they do not confer founder authority (residual: some still store a client-supplied `actor`/`created_by` label) |
| GET routes, `/health` | Read-only / public | unchanged |

## Learning memory: every path to APPROVED

The only code that assigns `LearningStatus.APPROVED` is `approve_learning` (founder-only). `POST /learnings`, incident
resolution, notebook compaction and `experiments.propose_learning` all write CANDIDATE, and a static test fails the build if
any `Learning(...)` is constructed without an explicit status or any other module assigns APPROVED. The model default is still
APPROVED (`app/models.py`, not touched): a future writer that omits `status=` would mint trusted memory, which is what the
static test guards. Existing APPROVED rows are not rewritten; reviewing legacy rows needs a separate, reviewed plan.
Effect: `companyctl resolve ... <learning>` now produces a CANDIDATE; it reaches agents only after the founder approves it
from the Founder Desk (`POST /learnings/{id}/approve`).

## Stage C pilot authorization

- Issue: `POST /stage-c/authorization {task_key, minutes}`, founder only. Stored value binds `task_key`, `repository`,
  `source_sha` (server-derived), `action = stage_c.code_write`, `issued_at`, `expires_at` (<= 60 min), `issued_by =
  founder:access:<email>`; `updated_by` must equal it. The endpoint-written `stage-c.authorization.issue` audit row (same
  actor and fields) must exist, so a forged row without the audit trail is invalid.
- Fail closed: legacy `issued_by == "founder"`, a missing audit row, a different task/SHA/repository/action, an expiry beyond 60
  minutes, an expired or consumed record all deny. Consumption stays one-shot under the row lock (PostgreSQL concurrency
  test: 4 racing spenders, exactly 1 wins).
- `control.authorize_pilot(..., founder=Principal.id)` raises `PermissionError` for anything but a founder id; `revoke` takes a
  founder or `operator:*` id. There is no actor default and no static credential.
- `ENABLE_AUTONOMOUS_CODE_WRITE` stays `false`; issuing an authorization enables nothing by itself.
- `companyctl stage-c authorize` now refuses (root on the host is not the verified founder) and prints the API to use.
  `companyctl stage-c revoke` still works for root and is recorded as `operator:host-root`.
- Not covered: `stage-c-host-acceptance` (`updated_by == "founder"`, written by `scripts/stage-c-host-wizard.sh`) is still a
  string-trusted founder attestation read by `stage_c_worker.ready()`. It needs the same principal treatment before Stage C
  activation (separate change: wizard + `stage_c_worker.ready`).

## Contract: Agent 4 (Stage C cleanup)

`a4/stage-c-cleanup-founder-identity` (127a237) already matches this contract: `TrustedBoundary.issue(request, approval_key)`
calls `require_founder(request)`; `_check_authority` calls `require_founder_approval(db, key, action=approval_action(task, digest),
task_id=..., now=...)`; live leases (including the task's own) are refused; an expired own lease is released only with the
exact holder and token after verified inactivity. Integration notes:
1. That branch carries its own copy of `app/approval_identity.py`. After this branch merges, drop the copy and rebase so
   `is_founder_id` / `require_founder_approval` come from main (the tightened id regex requires a real lowercase email).
2. Do not expose cleanup through any route or the worker/scheduler. A route must call `require_founder(request)` first and may
   hand only that request to `TrustedBoundary.issue`.
3. Independent security review is required before activation. `operator:*` approvals and the legacy string `founder` are rejected
   by `require_founder_approval` (tests in `tests/test_founder_authorization.py`).

## Contract: Agent 3 (Founder Desk and Experiment API)

Experiment routes (`POST /experiments`, `/experiments/{key}/{approve|start|close-window|evaluate|accept|reject|cancel|learning}`):
- Founder steps (`approve`, `accept`, `reject`): `who = approval_identity.require_founder(request)`; pass `who.id` as `founder`.
- All other steps: `approval_identity.principal_of(request).id` as the actor. Never read an actor/founder value from the body,
  headers or query, and never build `founder:access:...` yourself.
- Map `NotFounder` -> 403, `ExperimentError` -> 409; call `db.rollback()` on any exception (denied operations leave no rows).
- Keep the routes inside `api_auth.ENFORCE_ALWAYS` (already matched for the routes above); the scheduler never calls founder steps.
- Tests to satisfy: forged body actor, operator/service/admin callers 401/403 in enforce and log mode, valid assertion records
  `founder:access:<email>`, revoked assertion 401, denied request leaves experiment and audit rows unchanged.

Founder Desk controls for DONE/COMPLETE, learning approval, agent resume and Stage C authorization: POST the routes above from the
Access session; send any `actor`/`decided_by` if the UI wants (ignored). Response bodies include the verified id
(`approved_by`, `decided_by`, `issued_by`) to display.

## Operational effects and migration path

- `companyctl approve/reject` (local): LOW/MEDIUM work as `operator:local`; granting HIGH/CRITICAL needs the Desk. Rejecting works.
- `companyctl task-status ... REVIEW|DONE`, `companyctl phase N COMPLETE`, `companyctl stage-c authorize`: refused without a verified founder.
  No CLI path is added (no static secret, no impersonation); a founder-held Access service credential bound to the founder's
  email would be the safe CLI route and needs a founder decision.
- Agent resume is Desk/Access only; pause is unchanged.

## Revocation

A founder is revoked by removing the email from `CF_ACCESS_DESK_EMAILS` (or rotating the Access audience): verification then
fails closed on the next request (tested: allow-list removal -> 401, nothing written). Already-recorded decisions are not
retroactively invalidated.

## Residual risk

- `stage-c-host-acceptance` and the host wizard still trust `updated_by == "founder"` (see above).
- `models.Learning.status` defaults to APPROVED; protected by a static test only.
- Remaining routes (checkpoints, notes, decisions, incidents) store client-supplied labels; they confer no founder authority.
- `operator:local` is root on the host; it can still decide LOW/MEDIUM approvals and revoke an authorization.
- The audit table is not tamper-proof against direct database writers; the audit-row checks defend the API, not a DB attacker.
- One allow-list, no per-action roles; PostgreSQL 18 behavior is verified only by CI.
