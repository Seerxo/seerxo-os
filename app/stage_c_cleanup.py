"""Founder-controlled, audited cleanup of one abandoned Stage C attempt (TESTING -> ABANDONED).

Two operations, deliberately not wired to the worker, watchdog, API or CLI:

- `preview_cleanup`: strictly read-only. Facts, eligibility, exact refusals, the plan and a digest of the
  expected state the founder approves.
- `apply_cleanup`: one atomic transition after the task, its runs and the repository lease are locked
  (task -> runs by id -> lease, the order the worker uses) and every fact is re-validated against the
  previewed state. Uncertainty refuses. Nothing is deleted: retry_count, AgentRun rows, history, artifacts,
  worktrees, branches and consumed authorizations are untouched.

Lease policy (founder decision 2026-10-08): a live lease refuses, including one this task holds; cleanup never
releases an active lease because it targets the holder. An expired lease alone proves nothing: this task's own
expired lease is released only when execution is verifiably inactive (task READY, so no worker claim; no
STARTED run; the newest run has ended) and only for the exact holder and fencing token (`stage_c.release`).
A stale runner is then fenced: `holds()` is False for its token, and a newer holder's token never matches it.

Authority (app/approval_identity.py): a plain actor string proves nothing. Apply needs a `FounderAuthority`,
minted only by `TrustedBoundary.issue(request, ...)` after `approval_identity.require_founder(request)`
verified the founder, plus an approval that `approval_identity.require_founder_approval` accepts for exactly
this task and the digest of the previewed state (`verified_founder_decision`: GRANTED by
`founder:access:<email>` with the endpoint's matching `approval.decide` audit row). Legacy `"founder"`,
operator and service decisions refuse. The audit and stage actor is the verified `Principal.id`.
Caller commits, as elsewhere in stage_c.

BLOCKED FOR PRODUCTION: not wired to any route, worker, scheduler or script, and must not be until
approval_identity is on main and this integration has had independent security review. Until then
tests/test_stage_c_cleanup.py fails if anything wires this module or invokes the mint.
"""
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import approval_identity, audit, enums, models, stage_c
from app.stage_c_worker import REPOSITORY

HOLDER = "stage-c-runner"
APPROVAL_MAX_AGE = timedelta(hours=24)
EVIDENCE_REASON = "founder_audited_cleanup"


class CleanupRefused(RuntimeError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


_MINT = object()


@dataclass(frozen=True)
class FounderAuthority:
    """Capability, not a name. Only `TrustedBoundary.issue` can construct it."""
    approval_key: str
    principal_id: str  # the verified founder Principal.id that requested the cleanup
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._mint is not _MINT:
            raise PermissionError("FounderAuthority can only be issued by a trusted boundary")


class TrustedBoundary:
    """The only mint. It verifies the founder itself (401/403 otherwise); callers cannot pass an identity in."""
    @staticmethod
    def issue(request, approval_key: str) -> FounderAuthority:
        principal = approval_identity.require_founder(request)
        return FounderAuthority(approval_key, principal.id, _MINT)


@dataclass(frozen=True)
class Expected:
    """Everything apply re-checks; the founder approves the digest of exactly this."""
    task_key: str
    task_id: int
    repository: str | None
    status: str
    stage: str | None
    history_len: int
    retry_count: int
    run_count: int
    max_run_id: int
    started_runs: int
    latest_run_ended: bool
    lease_state: str  # free | ours_live | ours_expired | other_live | other_expired | malformed
    lease_token: int | None


@dataclass(frozen=True)
class Plan:
    expected: Expected
    state: str  # eligible | already_cleaned | refused
    refusals: list[str]
    actions: list[str]
    digest: str


def approval_action(task_key: str, digest: str) -> str:
    return f"stage-c.cleanup {task_key} {digest}"


def _digest(exp: Expected) -> str:
    return hashlib.sha256(json.dumps(asdict(exp), sort_keys=True).encode()).hexdigest()


def _lease(value, task_key: str, now: datetime) -> tuple[str, int | None]:
    if value is None:
        return "free", None
    if not isinstance(value, dict):
        return "malformed", None
    token, holder = value.get("token"), value.get("holder")
    if isinstance(token, bool) or not isinstance(token, int):
        return "malformed", None
    if not holder:
        return "free", token
    if not isinstance(holder, str):
        return "malformed", None
    try:
        expires = datetime.fromisoformat(value["expires_at"])
    except (KeyError, TypeError, ValueError):
        return "malformed", token
    if expires.tzinfo is None:
        return "malformed", token
    owner = "ours" if holder == f"{HOLDER}:{task_key}" else "other"
    return f"{owner}_{'live' if expires > now else 'expired'}", token


def _gather(db: Session, task_key: str, now: datetime, lock: bool) -> tuple[models.Task | None, Expected | None, list[str]]:
    """Facts and refusals. With lock=True: task row, then its runs by id, then the lease row (FOR UPDATE)."""
    q = select(models.Task).where(models.Task.key == task_key)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    task = db.scalar(q)
    if task is None:
        return None, None, ["task_not_found"]
    rq = select(models.AgentRun).where(models.AgentRun.task_id == task.id).order_by(models.AgentRun.id)
    if lock:
        rq = rq.with_for_update().execution_options(populate_existing=True)
    runs = list(db.scalars(rq))
    lq = select(models.ControlState).where(models.ControlState.key == stage_c.lease_key(REPOSITORY))
    if lock:
        lq = lq.with_for_update().execution_options(populate_existing=True)
    lease_row = db.scalar(lq)
    imp = (task.context or {}).get("improvement") or {}
    history = imp.get("history") or []
    repository = next((h.get("evidence", {}).get("repository") for h in history
                       if (h.get("evidence") or {}).get("repository")), None)
    lease_state, token = _lease(lease_row.value if lease_row else None, task.key, now)
    exp = Expected(task.key, task.id, repository, task.status.value, imp.get("stage"), len(history),
                   task.retry_count, len(runs), max((r.id for r in runs), default=0),
                   sum(r.status == enums.AgentRunStatus.STARTED for r in runs),
                   bool(runs and runs[-1].ended_at and runs[-1].status != enums.AgentRunStatus.STARTED),
                   lease_state, token)
    refusals = []
    if exp.status != "READY":
        refusals.append(f"task_status_not_ready:{exp.status}")
    if exp.stage != "TESTING":
        refusals.append(f"stage_not_testing:{exp.stage}")
    if exp.started_runs:
        refusals.append(f"started_run_present:{exp.started_runs}")
    if repository != REPOSITORY:
        refusals.append(f"repository_mismatch:{repository}")
    if lease_state in ("ours_live", "other_live", "malformed"):
        refusals.append(f"lease_{lease_state}")  # a live lease means execution may be in flight
    if lease_state == "ours_expired" and not exp.latest_run_ended:
        refusals.append("own_lease_execution_not_verified_inactive")  # expiry alone is not evidence
    return task, exp, refusals


def _already_cleaned(task: models.Task | None, exp: Expected | None) -> bool:
    return bool(task and exp.status == "CANCELLED" and exp.stage == "ABANDONED" and task.completed_at)


def preview_cleanup(db: Session, task_key: str, now: datetime | None = None) -> Plan | None:
    """Read-only (no flush, no lock, no write). None when the task does not exist."""
    now = now or datetime.now(timezone.utc)
    with db.no_autoflush:
        task, exp, refusals = _gather(db, task_key, now, lock=False)
    if task is None:
        return None
    if _already_cleaned(task, exp):
        return Plan(exp, "already_cleaned", [], [], _digest(exp))
    actions = [] if refusals else [
        "advance improvement stage TESTING -> ABANDONED", "set task READY -> CANCELLED, completed_at = now",
        "release expired repository lease (exact holder and token)" if exp.lease_state == "ours_expired"
        else "no lease to release",
        "write audit evidence", "preserve retry_count, runs, history, artifacts, worktrees, branches"]
    return Plan(exp, "refused" if refusals else "eligible", refusals, actions, _digest(exp))


def _check_authority(db: Session, authority, task: models.Task, digest: str, now: datetime) -> models.Approval:
    if not (isinstance(authority, FounderAuthority)
            and approval_identity.Principal("founder", authority.principal_id).is_founder):
        raise CleanupRefused(["founder_authority_missing"])
    try:
        return approval_identity.require_founder_approval(
            db, authority.approval_key, action=approval_action(task.key, digest), task_id=task.id,
            max_age=APPROVAL_MAX_AGE, now=now)
    except PermissionError as e:
        raise CleanupRefused([f"approval_not_verified_founder_decision:{e}"]) from None


def apply_cleanup(db: Session, expected: Expected, authority: FounderAuthority,
                  now: datetime | None = None) -> str:
    """'cleaned' or 'noop'. Raises CleanupRefused with nothing changed. Caller commits."""
    now = now or datetime.now(timezone.utc)
    task, current, refusals = _gather(db, expected.task_key, now, lock=True)
    if task is None:
        raise CleanupRefused(refusals)
    if _already_cleaned(task, current):
        return "noop"
    if current != expected:
        diff = [k for k, v in asdict(expected).items() if asdict(current)[k] != v]
        raise CleanupRefused([f"state_changed:{','.join(diff)}", *refusals])
    if refusals:
        raise CleanupRefused(refusals)
    apr = _check_authority(db, authority, task, _digest(expected), now)
    actor = authority.principal_id
    before = {"status": current.status, "stage": current.stage, "retry_count": current.retry_count,
              "lease_state": current.lease_state, "lease_token": current.lease_token}
    try:
        with db.begin_nested():  # a failure anywhere leaves task, lease and audit untouched
            stage_c.advance(db, task, "ABANDONED", actor, {
                "reason": EVIDENCE_REASON, "approval": apr.key, "approval_decided_by": apr.decided_by,
                "plan_digest": _digest(expected),
                "run_count": current.run_count, "max_run_id": current.max_run_id})
            task.status = enums.TaskStatus.CANCELLED
            task.completed_at = now
            released = current.lease_state == "ours_expired"
            if released:
                stage_c.release(db, REPOSITORY, current.lease_token)
            audit.record(db, actor=actor, action="stage-c.cleanup.apply", entity_type="task",
                         entity_key=task.key, task_id=task.id, reason=EVIDENCE_REASON, before=before,
                         after={"status": "CANCELLED", "stage": "ABANDONED", "lease_released": released,
                                "retry_count": task.retry_count, "plan_digest": _digest(expected),
                                "approval": apr.key, "approval_decided_by": apr.decided_by})
            db.flush()
    except Exception:
        db.expire_all()
        raise
    return "cleaned"
