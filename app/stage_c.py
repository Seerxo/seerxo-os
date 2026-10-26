"""Stage C building blocks (docs/AUTONOMOUS_AGENT_ARCHITECTURE.md §12). The worker adapter
requires explicit enablement and founder host acceptance; compose keeps writes OFF.

- Repository lease with a fencing token: one mutating task per repository. A holder whose
  lease expired (crash, timeout) loses it, and its stale token can no longer pass `holds()`,
  so it cannot push over the new holder's work.
- Isolated worktree per task, on branch task/<KEY>, from a local mirror.
- Diff fingerprint: the reviewed diff, the pushed branch and the PR head must be the same bytes.
- Improvement lifecycle recorded on the task (context["improvement"]) with an audit row.
"""
import hashlib
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, models

# What this module provides, reported in the worker heartbeat so the Desk can say
# "foundation present, runtime off" only when the running worker actually carries it.
FOUNDATION = ("lease", "worktree", "fingerprint", "lifecycle")

TASK_KEY = re.compile(r"^[A-Z]+-\d{1,6}$")

STAGES = ("DETECTED", "PLANNED", "IMPLEMENTING", "TESTING", "INDEPENDENT_REVIEW", "PR_OPEN",
          "CI", "READY_TO_MERGE", "MERGED", "VERIFYING", "MEASURED", "ABANDONED")
# Forward one step at a time; failed tests/review/CI go back to IMPLEMENTING (repairs are
# bounded by the caller); anything not yet merged may be abandoned. MERGED is the founder's.
NEXT = {
    "DETECTED": {"PLANNED"},
    "PLANNED": {"IMPLEMENTING"},
    "IMPLEMENTING": {"TESTING"},
    "TESTING": {"INDEPENDENT_REVIEW", "IMPLEMENTING"},
    "INDEPENDENT_REVIEW": {"PR_OPEN", "IMPLEMENTING"},
    "PR_OPEN": {"CI"},
    "CI": {"READY_TO_MERGE", "IMPLEMENTING"},
    "READY_TO_MERGE": {"MERGED", "IMPLEMENTING"},
    "MERGED": {"VERIFYING"},
    "VERIFYING": {"MEASURED"},
    "MEASURED": set(),
    "ABANDONED": set(),
}
for _s in STAGES[:STAGES.index("MERGED")]:
    NEXT[_s] = NEXT[_s] | {"ABANDONED"}


class LeaseLost(RuntimeError):
    pass


def lease_key(repository: str) -> str:
    return f"lock:repo:{repository}"


def _locked(db: Session, key: str) -> models.ControlState:
    if db.get(models.ControlState, key) is None:
        try:
            with db.begin_nested():
                db.add(models.ControlState(key=key, value={"token": 0}, updated_by="stage-c"))
                db.flush()
        except IntegrityError:
            pass  # created concurrently; lock that row
    return db.scalar(select(models.ControlState).where(models.ControlState.key == key)
                     .with_for_update().execution_options(populate_existing=True))


def _expired(value: dict, now: datetime) -> bool:
    return not value.get("holder") or datetime.fromisoformat(value["expires_at"]) <= now


def acquire(db: Session, repository: str, holder: str, ttl: timedelta,
            now: datetime | None = None) -> int | None:
    """Fencing token for `holder`, or None while another holder's lease is live. Caller commits."""
    now = now or datetime.now(timezone.utc)
    row = _locked(db, lease_key(repository))
    value = dict(row.value)
    if not _expired(value, now) and value["holder"] != holder:
        return None
    value.update(holder=holder, token=int(value.get("token", 0)) + 1,
                 expires_at=(now + ttl).isoformat())
    row.value, row.updated_by = value, holder
    return value["token"]


def holds(db: Session, repository: str, token: int, now: datetime | None = None) -> bool:
    """True only for the current, unexpired token. Check under the row lock right before a push."""
    now = now or datetime.now(timezone.utc)
    value = _locked(db, lease_key(repository)).value
    return value.get("token") == token and not _expired(value, now)


def release(db: Session, repository: str, token: int) -> None:
    row = _locked(db, lease_key(repository))
    if row.value.get("token") != token:
        raise LeaseLost(f"{repository}: token {token} is no longer current")
    row.value = {"token": token}  # keep the counter monotonic


def diff_fingerprint(diff: bytes) -> str:
    return hashlib.sha256(diff).hexdigest()


def prepare_worktree(mirror: Path, workspaces: Path, task_key: str, base: str) -> Path:
    """New worktree at workspaces/<KEY> on branch task/<KEY> from `base` in the local mirror."""
    if not TASK_KEY.fullmatch(task_key):
        raise ValueError(f"invalid task key {task_key!r}")
    path = workspaces / task_key
    if path.exists():
        raise FileExistsError(f"{path} already exists; a previous run must be cleaned up first")
    subprocess.run(["git", "-C", str(mirror), "worktree", "add", "-q", "-b", f"task/{task_key}",
                    str(path), base], check=True, capture_output=True, text=True, timeout=120)
    return path


def advance(db: Session, task: models.Task, stage: str, actor: str,
            evidence: dict | None = None) -> None:
    """Record the next improvement stage on the task. Raises on a skipped or backward step."""
    context = dict(task.context or {})
    imp = dict(context.get("improvement") or {"stage": None, "history": []})
    current = imp.get("stage")
    allowed = {"DETECTED"} if current is None else NEXT[current]
    if stage not in allowed:
        raise ValueError(f"{task.key}: {current} -> {stage} is not allowed")
    # Only the founder merges. Automation may record that act only from GitHub evidence
    # (stage_c_ci: merged_by is the founder's login) and must say so in the actor.
    if stage == "MERGED" and actor != "founder" and not (
            actor.startswith("founder (github:") and (evidence or {}).get("merged_by")
            and (evidence or {}).get("merge_commit")):
        raise PermissionError("only the founder merges")
    if stage == "IMPLEMENTING":
        imp.pop("approval", None)
    imp["history"] = [*imp.get("history", []), {"stage": stage, "actor": actor,
                                        "at": datetime.now(timezone.utc).isoformat(),
                                        "evidence": evidence or {}}]
    imp["stage"] = stage
    context["improvement"] = imp
    task.context = context
    audit.record(db, actor=actor, action="improvement.advance", entity_type="task",
                 entity_key=task.key, before={"stage": current},
                 after={"stage": stage, "evidence": evidence or {}},
                 task_id=task.id)
