"""Persistent controls for agent execution. Production services are never touched."""
import fcntl
import os
import re
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import approval_identity, audit, models

_PAUSE_KEY = "agents_paused"
CODER_PAUSE_FENCE = "/run/company-coder-pause.lock"
_FENCE_UID = 0
_FENCE_TIMEOUT = 10


class PauseUnavailable(RuntimeError):
    pass


def open_coder_pause_fence() -> int:
    fd = os.open(CODER_PAUSE_FENCE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != _FENCE_UID or st.st_mode & 0o222 or st.st_nlink != 1:
        os.close(fd)
        raise OSError("invalid coder pause fence")
    return fd


@contextmanager
def coder_pause_fence():
    # Legacy LEVEL 1 works without this mount; the root launcher refuses unless it is enabled.
    if os.environ.get("CODER_PAUSE_FENCE_ENABLED") != "true":
        yield
        return
    try:
        fd = open_coder_pause_fence()
        try:
            deadline = time.monotonic() + _FENCE_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PauseUnavailable("coder admission is busy; pause was not applied, retry")
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)
    except OSError as exc:
        raise PauseUnavailable("coder pause fence is unavailable; pause was not applied") from exc


def agents_paused(db: Session) -> bool:
    row = db.get(models.ControlState, _PAUSE_KEY, populate_existing=True)
    return bool((row.value or {}).get("paused")) if row else False


def set_agents_paused(db: Session, paused: bool, *, actor: str, reason: str) -> dict:
    with coder_pause_fence():
        return _set_agents_paused(db, paused, actor=actor, reason=reason)


def _set_agents_paused(db: Session, paused: bool, *, actor: str, reason: str) -> dict:
    row = db.get(models.ControlState, _PAUSE_KEY)
    before = dict(row.value) if row else {"paused": False}
    if row is None:
        row = models.ControlState(key=_PAUSE_KEY, value={}, updated_by=actor)
        db.add(row)
    row.value = {"paused": paused, "reason": reason}
    row.updated_by = actor
    audit.record(
        db, actor=actor, action="agents.pause" if paused else "agents.resume",
        entity_type="control_state", entity_key=_PAUSE_KEY, reason=reason,
        before=before, after=row.value,
    )
    db.commit()
    db.refresh(row)
    return {"paused": paused, "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat(), "reason": reason}


# --------------------------------------------------------------------------- Stage C attended pilot authorization
# ENABLE_AUTONOMOUS_CODE_WRITE is a global capability. A code write also needs this founder-issued
# authorization: one task, one repository, one deployed SHA, short-lived, spent when an attempt claims it.
PILOT_AUTHORIZATION = "stage-c-pilot-authorization"
PILOT_ACTION = "stage_c.code_write"
AUTH_REPOSITORY = "seerxo/os"
AUTH_MAX_TTL = timedelta(hours=1)
_TASK_KEY = re.compile(r"^[A-Z]+-\d{1,6}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")


def _utc(text) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _issue_audited(db: Session, value: dict, *, action="stage-c.authorization.issue", key=PILOT_AUTHORIZATION,
                   fields=("task_key", "source_sha", "issued_at", "expires_at", "issued_by")) -> bool:
    """The endpoint-written issue audit row for exactly this authorization (same actor, task, SHA, times)."""
    entry = db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == action, models.AuditLog.entity_key == key,
        models.AuditLog.actor == value.get("issued_by")).order_by(models.AuditLog.id.desc()).limit(1)).first()
    after = entry.after if entry is not None and isinstance(entry.after, dict) else {}
    return all(after.get(k) == value.get(k) for k in fields)


def _authorization_denial(db: Session, row, task_key: str, sha: str, now: datetime) -> str | None:
    """Deterministic, model-free. Anything missing, malformed or unknown is a denial. Authority is the verified
    founder principal id recorded at issue; the legacy string "founder" is not an authorization."""
    if row is None:
        return "stage_c_authorization_missing"
    value = row.value if isinstance(row.value, dict) else {}
    issued, expires = _utc(value.get("issued_at")), _utc(value.get("expires_at"))
    issuer = value.get("issued_by")
    if (not approval_identity.is_founder_id(issuer) or row.updated_by != issuer
            or value.get("action") != PILOT_ACTION or not issued or not expires
            or not _SHA.match(sha or "") or value.get("repository") != AUTH_REPOSITORY
            or value.get("task_key") != task_key or value.get("source_sha") != sha
            or not (timedelta(0) < expires - issued <= AUTH_MAX_TTL) or issued > now + timedelta(minutes=1)
            or not _issue_audited(db, value)):
        return "stage_c_authorization_invalid"
    if value.get("consumed_at") is not None:
        return "stage_c_authorization_consumed"
    if now >= expires:
        return "stage_c_authorization_expired"
    return None


def pilot_authorization_denial(db: Session, task_key: str, sha: str, now: datetime | None = None) -> str | None:
    return _authorization_denial(db, db.get(models.ControlState, PILOT_AUTHORIZATION), task_key, sha,
                                 now or datetime.now(timezone.utc))


def authorize_pilot(db: Session, task_key: str, sha: str, *, founder: str, ttl: timedelta = timedelta(minutes=30),
                    now: datetime | None = None) -> dict:
    """Record the authorization for the verified founder `founder` (a Principal.id from require_founder).
    It enables nothing by itself: the flag, acceptance and readiness still apply. Anything but a founder
    principal id raises PermissionError and writes nothing."""
    if not approval_identity.is_founder_id(founder):
        raise PermissionError("only a verified founder identity may issue a Stage C authorization")
    now = now or datetime.now(timezone.utc)
    if not _TASK_KEY.match(task_key) or not _SHA.match(sha) or not (timedelta(0) < ttl <= AUTH_MAX_TTL):
        raise ValueError("authorization needs a task key, a 40-hex source SHA and a ttl up to one hour")
    if db.scalar(select(models.Task.id).where(models.Task.key == task_key)) is None:
        raise ValueError(f"{task_key} does not exist")
    row = db.scalar(select(models.ControlState).where(models.ControlState.key == PILOT_AUTHORIZATION)
                    .with_for_update().execution_options(populate_existing=True))
    before = dict(row.value) if row and isinstance(row.value, dict) else None
    value = {"task_key": task_key, "repository": AUTH_REPOSITORY, "source_sha": sha, "action": PILOT_ACTION,
             "issued_at": now.isoformat(), "expires_at": (now + ttl).isoformat(), "issued_by": founder,
             "consumed_at": None, "consumed_by_run_id": None}
    if row is None:
        row = models.ControlState(key=PILOT_AUTHORIZATION, value=value, updated_by=founder)
        db.add(row)
    else:
        row.value, row.updated_by = value, founder
    audit.record(db, actor=founder, action="stage-c.authorization.issue", entity_type="control_state",
                 entity_key=PILOT_AUTHORIZATION, reason=f"{task_key} @ {sha[:7]}", before=before, after=value)
    db.commit()
    return value


def revoke_pilot_authorization(db: Session, *, actor: str, reason: str = "revoked") -> bool:
    """Withdrawing authority is the safe direction, so a verified founder or the host operator may do it;
    `actor` is a Principal.id (never a body string) and is what the audit row records."""
    if not isinstance(actor, str) or not (approval_identity.is_founder_id(actor) or actor.startswith("operator:")):
        raise PermissionError("revocation needs a verified founder or operator identity")
    row = db.get(models.ControlState, PILOT_AUTHORIZATION)
    if row is None:
        return False
    before = dict(row.value) if isinstance(row.value, dict) else None
    db.delete(row)
    audit.record(db, actor=actor, action="stage-c.authorization.revoke", entity_type="control_state",
                 entity_key=PILOT_AUTHORIZATION, reason=reason, before=before, after=None)
    db.commit()
    return True


def consume_pilot_authorization(db: Session, task_key: str, sha: str, run_id: int,
                         now: datetime | None = None) -> str | None:
    """Spend the authorization for this attempt, or return the denial. The row is locked and re-validated in
    the caller's transaction, so two claimants cannot both spend it. Caller commits."""
    now = now or datetime.now(timezone.utc)
    row = db.scalar(select(models.ControlState).where(models.ControlState.key == PILOT_AUTHORIZATION)
                    .with_for_update().execution_options(populate_existing=True))
    denial = _authorization_denial(db, row, task_key, sha, now)
    if denial:
        return denial
    before = dict(row.value)
    row.value = {**before, "consumed_at": now.isoformat(), "consumed_by_run_id": run_id}
    audit.record(db, actor="company-worker", action="stage-c.authorization.consume", entity_type="control_state",
                 entity_key=PILOT_AUTHORIZATION, reason=f"{task_key} run {run_id}", before=before, after=row.value)
    return None


# --------------------------------------------------------------------------- Stage C standing authorization
# The one-task pilot authorization above stays the strictest form. A standing authorization lets the founder
# approve ONCE a short list of allow-listed files for a few days and a few runs a day, so the worker does not
# need a new click per task. It is the same authority, only wider in time: founder-issued from a verified
# principal, bound to the deployed SHA (any deploy voids it, so a new release needs a new decision),
# limited to named files, capped per UTC day, revocable, and spent one run at a time under a row lock.
# It enables nothing by itself: the global flag, host acceptance and runtime readiness still apply, and
# merge and deploy stay outside it.
STANDING_AUTHORIZATION = "stage-c-standing-authorization"
STANDING_ACTION = "stage_c.code_write.standing"
STANDING_MAX_TTL = timedelta(days=7)
STANDING_MAX_PER_DAY = 3
_STANDING_FIELDS = ("source_sha", "paths", "max_per_day", "issued_at", "expires_at", "issued_by")


def _task_paths(task):
    scope = (task.context or {}).get("stage_c") if isinstance(task.context, dict) else None
    return scope.get("paths") if isinstance(scope, dict) else None


def _standing_denial(db: Session, row, wanted, sha: str, now: datetime) -> str | None:
    """Deterministic and model-free; anything missing, malformed or out of scope is a denial."""
    if row is None:
        return "stage_c_standing_missing"
    value = row.value if isinstance(row.value, dict) else {}
    issued, expires = _utc(value.get("issued_at")), _utc(value.get("expires_at"))
    issuer, paths, cap = value.get("issued_by"), value.get("paths"), value.get("max_per_day")
    if (not approval_identity.is_founder_id(issuer) or row.updated_by != issuer
            or value.get("action") != STANDING_ACTION or not issued or not expires
            or not _SHA.match(sha or "") or value.get("repository") != AUTH_REPOSITORY
            or value.get("source_sha") != sha
            or not isinstance(paths, list) or not 1 <= len(paths) <= 20
            or not all(isinstance(p, str) and p for p in paths)
            or isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= STANDING_MAX_PER_DAY
            or not (timedelta(0) < expires - issued <= STANDING_MAX_TTL) or issued > now + timedelta(minutes=1)
            or not _issue_audited(db, value, action="stage-c.standing.issue", key=STANDING_AUTHORIZATION,
                                  fields=_STANDING_FIELDS)):
        return "stage_c_standing_invalid"
    if now >= expires:
        return "stage_c_standing_expired"
    if not isinstance(wanted, list) or not wanted or not set(wanted) <= set(paths):
        return "stage_c_standing_scope"
    used = value.get("used") if isinstance(value.get("used"), dict) else {}
    if used.get("day") == now.date().isoformat() and isinstance(used.get("count"), int) and used["count"] >= cap:
        return "stage_c_standing_daily_limit"
    return None


def write_authorization_denial(db: Session, task, sha: str, now: datetime | None = None) -> str | None:
    """None when this task may claim a code write: its own one-task authorization, else the standing one."""
    now = now or datetime.now(timezone.utc)
    pilot = pilot_authorization_denial(db, task.key, sha, now)
    if pilot is None:
        return None
    row = db.get(models.ControlState, STANDING_AUTHORIZATION)
    standing = _standing_denial(db, row, _task_paths(task), sha, now)
    if standing is None:
        return None
    # Report the form the founder actually issued; with neither, the pilot wording stays (existing behaviour).
    return pilot if row is None or db.get(models.ControlState, PILOT_AUTHORIZATION) is not None else standing


def issue_standing(db: Session, paths: list[str], sha: str, *, founder: str, days: int = 3, max_per_day: int = 1,
                   now: datetime | None = None) -> dict:
    """Record the standing authorization for a verified founder principal. Writes nothing unless valid."""
    from app import stage_c_worker  # late: stage_c_worker imports this module
    if not approval_identity.is_founder_id(founder):
        raise PermissionError("only a verified founder identity may issue a Stage C standing authorization")
    now = now or datetime.now(timezone.utc)
    ttl = timedelta(days=days)
    if (not _SHA.match(sha) or not isinstance(paths, list) or not 1 <= len(paths) <= 20
            or len(set(paths)) != len(paths) or not all(p in stage_c_worker.SAFE_FILES for p in paths)
            or isinstance(max_per_day, bool) or not 1 <= max_per_day <= STANDING_MAX_PER_DAY
            or not timedelta(0) < ttl <= STANDING_MAX_TTL):
        raise ValueError("a standing authorization needs 1-20 allow-listed files, a 40-hex SHA, up to "
                         f"{STANDING_MAX_PER_DAY} runs a day and up to {STANDING_MAX_TTL.days} days")
    row = db.scalar(select(models.ControlState).where(models.ControlState.key == STANDING_AUTHORIZATION)
                    .with_for_update().execution_options(populate_existing=True))
    before = dict(row.value) if row and isinstance(row.value, dict) else None
    value = {"repository": AUTH_REPOSITORY, "source_sha": sha, "action": STANDING_ACTION, "paths": sorted(paths),
             "max_per_day": max_per_day, "issued_at": now.isoformat(), "expires_at": (now + ttl).isoformat(),
             "issued_by": founder, "used": {"day": None, "count": 0}}
    if row is None:
        row = models.ControlState(key=STANDING_AUTHORIZATION, value=value, updated_by=founder)
        db.add(row)
    else:
        row.value, row.updated_by = value, founder
    audit.record(db, actor=founder, action="stage-c.standing.issue", entity_type="control_state",
                 entity_key=STANDING_AUTHORIZATION, reason=f"{len(paths)} file(s), {max_per_day}/day, {days}d @ {sha[:7]}",
                 before=before, after=value)
    db.commit()
    return value


def revoke_standing(db: Session, *, actor: str, reason: str = "revoked") -> bool:
    """Withdrawing authority is the safe direction: a verified founder or the host operator may do it."""
    if not isinstance(actor, str) or not (approval_identity.is_founder_id(actor) or actor.startswith("operator:")):
        raise PermissionError("revocation needs a verified founder or operator identity")
    row = db.get(models.ControlState, STANDING_AUTHORIZATION)
    if row is None:
        return False
    before = dict(row.value) if isinstance(row.value, dict) else None
    db.delete(row)
    audit.record(db, actor=actor, action="stage-c.standing.revoke", entity_type="control_state",
                 entity_key=STANDING_AUTHORIZATION, reason=reason, before=before, after=None)
    db.commit()
    return True


def consume_write_authorization(db: Session, task, sha: str, run_id: int, now: datetime | None = None) -> str | None:
    """Spend the one-task authorization if it is valid, else one run of the standing one. Caller commits."""
    now = now or datetime.now(timezone.utc)
    if consume_pilot_authorization(db, task.key, sha, run_id, now) is None:
        return None
    row = db.scalar(select(models.ControlState).where(models.ControlState.key == STANDING_AUTHORIZATION)
                    .with_for_update().execution_options(populate_existing=True))
    denial = _standing_denial(db, row, _task_paths(task), sha, now)
    if denial:
        return write_authorization_denial(db, task, sha, now) or denial
    before = dict(row.value)
    used = before.get("used") if isinstance(before.get("used"), dict) else {}
    day = now.date().isoformat()
    count = (used.get("count") if used.get("day") == day and isinstance(used.get("count"), int) else 0) + 1
    row.value = {**before, "used": {"day": day, "count": count}}
    audit.record(db, actor="company-worker", action="stage-c.standing.consume", entity_type="control_state",
                 entity_key=STANDING_AUTHORIZATION, reason=f"{task.key} run {run_id} ({count} today)",
                 before=before, after=row.value)
    return None


def refund_standing_run(db: Session, task, run_id: int, now: datetime | None = None) -> bool:
    """Give back the standing run a claim spent when the attempt then failed only on provider capacity (quota,
    rate limit, auth): nothing was produced, and an unavailable provider must not use up the founder's daily
    allowance. Exactly-once: only a run whose own consume audit exists, and only once. Caller commits."""
    now = now or datetime.now(timezone.utc)
    reason = f"{task.key} run {run_id} "
    spent = db.scalar(select(models.AuditLog.id).where(
        models.AuditLog.action == "stage-c.standing.consume", models.AuditLog.entity_key == STANDING_AUTHORIZATION,
        models.AuditLog.reason.like(reason + "(%")))
    refunded = db.scalar(select(models.AuditLog.id).where(
        models.AuditLog.action == "stage-c.standing.refund", models.AuditLog.entity_key == STANDING_AUTHORIZATION,
        models.AuditLog.reason.like(reason + "%")))
    row = db.scalar(select(models.ControlState).where(models.ControlState.key == STANDING_AUTHORIZATION)
                    .with_for_update().execution_options(populate_existing=True))
    if not spent or refunded or row is None or not isinstance(row.value, dict):
        return False
    used = row.value.get("used") if isinstance(row.value.get("used"), dict) else {}
    count = used.get("count")
    if used.get("day") != now.date().isoformat() or not isinstance(count, int) or count < 1:
        return False  # the day rolled over: that run is no longer counted
    before = dict(row.value)
    row.value = {**before, "used": {"day": used["day"], "count": count - 1}}
    audit.record(db, actor="company-worker", action="stage-c.standing.refund", entity_type="control_state",
                 entity_key=STANDING_AUTHORIZATION, reason=f"{reason}(provider capacity failure; {count - 1} today)",
                 before=before, after=row.value)
    return True


def standing_covers(db: Session, paths: list[str], sha: str, now: datetime | None = None) -> bool:
    """Is every file in `paths` inside a currently valid standing authorization for this deployed SHA?"""
    return _standing_denial(db, db.get(models.ControlState, STANDING_AUTHORIZATION), paths, sha,
                            now or datetime.now(timezone.utc)) is None
