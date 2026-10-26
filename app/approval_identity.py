"""Who decided? A verified principal, never a name the client typed.

Threat model (2026-10-08): `POST /approvals/{key}` used to store `decided_by` from the request body, so any
caller allowed to reach it could record "founder". Admin, host, local, desk-token and youtube-token callers
prove possession of a secret or a network position; none proves a human founder. The ONLY proof of the
founder is a verified Cloudflare Access assertion (fixed issuer/JWKS, audience, allow-listed email), which
api_auth turns into a founder Principal. Everything else is an operator or service principal.

Contract (protected authorization boundary):
- `Principal.id` is the only value that may be written to `decided_by`, audit actors or any "who approved"
  field. Founder ids are always `founder:access:<email>`; no client-controlled string can produce that prefix
  because request bodies are never used for identity.
- Founder-only actions (approve/reject, experiment READY and accept/reject decisions, and any later cleanup
  trigger) call `require_founder(request)` and use its `.id`. Operators (`operator:local`, `operator:host`) may
  decide approvals but are recorded as themselves and never count as founder authority.
- Code that consumes an approval as founder authority (Stage C cleanup, Experiment Engine) must call
  `verified_founder_decision(db, approval)`: the approval is GRANTED, `decided_by` carries the founder prefix,
  and the audit trail holds the matching `approval.decide` row written by the endpoint. A legacy row with
  `decided_by == "founder"` is NOT founder authority.
- Denials write nothing (no approval, task or audit row): they only log.
- Sensitive routes (api_auth.ENFORCE_ALWAYS) are enforced even when API_AUTH_MODE=log.
- High and critical risk approvals can be granted only by the verified founder (an operator may reject them).
- Task DONE and phase COMPLETE need `require_founder`; `actor` fields in bodies are untrusted metadata.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, models

FOUNDER_PREFIX = "founder:access:"
FOUNDER_ONLY_RISKS = (enums.RiskLevel.HIGH, enums.RiskLevel.CRITICAL)  # an operator may reject these, never grant
APPROVAL_MAX_AGE = timedelta(hours=24)
MAX_ID = 100  # Approval.decided_by is String(100): an id that does not fit is refused, never truncated


@dataclass(frozen=True)
class Principal:
    kind: str  # founder | operator | service | anonymous
    id: str

    @property
    def is_founder(self) -> bool:
        return self.kind == "founder" and is_founder_id(self.id)


ANONYMOUS = Principal("anonymous", "anonymous")
_FOUNDER_ID = re.compile(re.escape(FOUNDER_PREFIX) + r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")


def is_founder_id(value) -> bool:
    """Format guard for a stored/passed Principal.id: exactly `founder:access:<lowercase email>`. It is not
    authentication; callers must get the id from `require_founder(request)`."""
    return isinstance(value, str) and len(value) <= MAX_ID and _FOUNDER_ID.fullmatch(value) is not None


def founder(email: str) -> Principal:
    return Principal("founder", f"{FOUNDER_PREFIX}{email.strip().lower()}")


def from_caller(who: str) -> Principal:
    """Principal for a non-Access caller: names the credential or position, never a person."""
    if who in ("local", "host"):
        return Principal("operator", f"operator:{who}")
    if who == "anonymous":
        return ANONYMOUS
    return Principal("service", f"service:{who}")


def principal_of(request: Request | None) -> Principal:
    """A direct in-process call has no request and therefore no identity: anonymous, which fails closed."""
    p = getattr(getattr(request, "state", None), "principal", None)
    return p if isinstance(p, Principal) else ANONYMOUS  # no middleware, no identity


def _deny(principal: Principal, why: str):
    raise HTTPException(401 if principal.kind == "anonymous" else 403, why)


def require_founder(request: Request) -> Principal:
    p = principal_of(request)
    if not p.is_founder or len(p.id) > MAX_ID:
        _deny(p, "a verified founder identity is required")
    return p


def require_decider(request: Request) -> Principal:
    """Who may record an approval decision: the verified founder, or the host operator as itself."""
    p = principal_of(request)
    if not (p.is_founder or p.kind == "operator") or len(p.id) > MAX_ID:
        _deny(p, "only the verified founder or the host operator may decide approvals")
    return p


def verified_founder_decision(db: Session, approval: models.Approval) -> bool:
    """True only for a GRANTED approval whose decision the endpoint recorded for a verified founder."""
    if (approval.status != enums.ApprovalStatus.GRANTED or not approval.decided_by
            or not is_founder_id(approval.decided_by) or approval.decided_at is None):
        return False
    entry = db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "approval.decide", models.AuditLog.entity_type == "approval",
        models.AuditLog.entity_key == approval.key, models.AuditLog.actor == approval.decided_by
    ).order_by(models.AuditLog.id.desc()).limit(1)).first()
    return bool(entry and (entry.after or {}).get("status") == enums.ApprovalStatus.GRANTED.value)


def require_founder_approval(db: Session, key: str, *, action: str, task_id: int | None = None,
                             max_age: timedelta = APPROVAL_MAX_AGE, now: datetime | None = None) -> models.Approval:
    """The approval `key` as founder authority for exactly `action` (and `task_id`), or PermissionError.

    Fail-closed single call for any consumer of a founder-only approval: verified founder decision, same
    action text, same task, not older than `max_age`. Anything else (operator decision, other action,
    unknown key, stale) refuses.
    """
    now = now or datetime.now(timezone.utc)
    apr = db.scalar(select(models.Approval).where(models.Approval.key == key))
    if apr is None or not verified_founder_decision(db, apr):
        raise PermissionError("no verified founder decision for this approval")
    if apr.action != action or (task_id is not None and apr.task_id != task_id):
        raise PermissionError("the approval is for a different action or task")
    decided = apr.decided_at if apr.decided_at.tzinfo else apr.decided_at.replace(tzinfo=timezone.utc)
    if decided > now or now - decided > max_age:
        raise PermissionError("the approval is stale or future-dated")
    return apr
