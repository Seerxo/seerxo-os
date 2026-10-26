"""C1: deterministic repository observation, never repository execution.

GitHub evidence -> existing Project/ControlState/Task/Event/Audit primitives.
No provider, shell, worktree, push, approval or production capability lives here.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, control, enums, keys, models, stage_c

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_OPEN_TASKS = 2


class Check(BaseModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: Literal["queued", "in_progress", "completed"]
    conclusion: str | None = Field(default=None, max_length=40)


class Snapshot(BaseModel):
    repository: str = Field(max_length=200)
    main_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    observed_at: datetime
    changed_files: list[str] = Field(default_factory=list, max_length=100)
    diff_complete: bool = False
    checks_complete: bool = False
    checks: list[Check] = Field(default_factory=list, max_length=100)


def repository_name(value: str | None) -> str | None:
    """Exact canonical metadata only; never guess a URL from descriptive prose."""
    value = value or ""
    if value.startswith("https://github.com/"):
        value = value[len("https://github.com/"):]
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower() if REPOSITORY.fullmatch(value) and all(p not in {".", ".."} for p in value.split("/")) else None


def checkpoint_key(repository: str) -> str:
    return "repo-guardian:" + hashlib.sha256(repository.encode()).hexdigest()


def _external_context(evidence: dict) -> dict:
    raw = json.dumps(evidence)
    # Context Builder already carries required_tool_state to attended Herdr.
    # Escape delimiters so external paths/check names cannot mimic control tags.
    inert = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return {"external_inputs": [raw], "required_tool_state": [
        {"source": "github", "trust": "external_untrusted", "evidence_json": inert}]}


def _locked_checkpoint(db: Session, repository: str) -> models.ControlState:
    key = checkpoint_key(repository)
    if db.get(models.ControlState, key) is None:
        try:
            with db.begin_nested():
                db.add(models.ControlState(key=key, value={"repository": repository},
                                           updated_by="repo-guardian"))
                db.flush()
        except IntegrityError:
            pass  # another controller created the unique checkpoint; lock that row
    return db.scalar(select(models.ControlState).where(models.ControlState.key == key)
                     .with_for_update().execution_options(populate_existing=True))


def reconcile(db: Session, project_key: str, facts: dict, *, allowlist: set[str],
              enabled: bool = False, apply: bool = True, now: datetime | None = None) -> dict:
    """One bounded reconciliation. Caller owns rollback on error; commit is atomic.

    Task risk is MEDIUM/attended until a root cause and bounded implementation are
    reviewed. A failed CI run is evidence to investigate, not proof of a product bug.
    """
    def skip(reason):
        return {"skipped": reason, "created": 0, "changed": False}

    if not enabled:
        return skip("disabled")
    if control.agents_paused(db):
        return skip("paused")
    project = db.scalar(select(models.Project).where(models.Project.key == project_key))
    if not project or project.status != enums.ProjectStatus.ACTIVE:
        return skip("project_not_active")
    unit = db.get(models.BusinessUnit, project.business_unit_id) if project.business_unit_id else None
    if unit and unit.status != enums.BusinessUnitStatus.ACTIVE:
        return skip("business_unit_not_active")
    repository = repository_name(project.repository)
    if not repository or repository not in allowlist:
        return skip("repository_not_allowed")
    snapshot = Snapshot.model_validate(facts)
    if snapshot.repository != repository:
        return skip("repository_mismatch")
    now = now or datetime.now(timezone.utc)
    if not snapshot.observed_at.tzinfo or not -timedelta(minutes=1) <= now - snapshot.observed_at <= timedelta(minutes=15):
        return skip("stale_evidence")
    if any(len(p) > 300 or p.startswith("/") or ".." in p.split("/") for p in snapshot.changed_files):
        raise ValueError("invalid changed scope")
    if apply and db.get_bind().dialect.name == "postgresql":
        # Merged != migrated: do not create recurring work before live DB safety exists.
        indexes = {i["name"] for i in inspect(db.connection()).get_indexes("tasks")}
        if "uq_tasks_open_dedupe" not in indexes:
            return skip("idempotency_migration_required")
    row = (_locked_checkpoint(db, repository) if apply
           else db.get(models.ControlState, checkpoint_key(repository)))
    before = dict(row.value) if row else {}
    if before.get("last_observed_at") and snapshot.observed_at < datetime.fromisoformat(before["last_observed_at"]):
        return skip("stale_evidence")
    previous = before.get("last_seen_main_sha")
    if previous and previous != snapshot.main_sha and snapshot.diff_base_sha != previous:
        return skip("stale_checkpoint")
    changed = bool(previous and previous != snapshot.main_sha)
    matching = [c for c in snapshot.checks if c.head_sha == snapshot.main_sha]
    failures = [c for c in matching if c.status == "completed" and c.conclusion in
                {"failure", "timed_out", "action_required"}]
    out = {"created": 0, "changed": changed, "findings": len(failures), "tasks": [], "mode": "apply" if apply else "dry-run"}
    if not apply:
        return out
    # Recheck controls after acquiring the checkpoint lock.
    db.refresh(project)
    db.expire_all()  # refresh pause policy even if another controller changed it
    if repository_name(project.repository) != repository:
        db.rollback()
        return skip("project_scope_changed")
    unit = db.get(models.BusinessUnit, project.business_unit_id) if project.business_unit_id else None
    if control.agents_paused(db) or project.status != enums.ProjectStatus.ACTIVE:
        db.rollback()
        return skip("paused_or_project_changed")
    if unit and unit.status != enums.BusinessUnitStatus.ACTIVE:
        db.rollback()
        return skip("business_unit_not_active")
    agent = db.scalar(select(models.Agent).where(models.Agent.key == "developer", models.Agent.enabled.is_(True)))
    reported = dict(before.get("reported_failures", {}))
    existing_work = list(db.scalars(select(models.Task).where(
        models.Task.created_by == "repo-guardian",
        models.Task.context["guardian"]["evidence"]["repository"].as_string() == repository,
        models.Task.status.in_(enums.OPEN_TASK_STATES))))
    for check in failures[:MAX_OPEN_TASKS]:
        fingerprint = hashlib.sha256(f"{repository}:REPAIR:CI:{check.name}".encode()).hexdigest()
        dedupe = f"repo-guardian:{fingerprint}"
        evidence = {"repository": repository, "base_sha": snapshot.diff_base_sha,
                    "head_sha": snapshot.main_sha, "check_id": check.id,
                    "check_name": check.name, "conclusion": check.conclusion,
                    "files": snapshot.changed_files, "diff_complete": snapshot.diff_complete,
                    "observed_at": snapshot.observed_at.isoformat()}
        existing = db.scalar(select(models.Task).where(models.Task.dedupe_key == dedupe,
                                                      models.Task.status.in_(enums.OPEN_TASK_STATES)))
        if existing:
            if reported.get(fingerprint) != snapshot.main_sha:
                context = dict(existing.context or {})
                finding = dict(context.get("guardian", {}))
                old_evidence = finding.get("evidence")
                finding.setdefault("original_evidence", old_evidence)
                finding["evidence"] = evidence
                existing.context = {**context, "guardian": finding,
                                    **_external_context(evidence)}
                audit.record(db, actor="repo-guardian", action="task.guardian_evidence",
                             entity_type="task", entity_key=existing.key, task_id=existing.id,
                             before=old_evidence, after=evidence, reason="Same finding on a new main commit")
            out["tasks"].append(existing.key)
            reported[fingerprint] = snapshot.main_sha
            continue
        if reported.get(fingerprint) == snapshot.main_sha:
            continue  # founder already handled this finding on this exact commit
        if not agent or len(existing_work) >= MAX_OPEN_TASKS:
            continue
        finding = {"type": "REPAIR", "severity": "MEDIUM", "evidence": evidence,
                   "why_it_matters": "Failing current-main CI prevents verified engineering progress.",
                   "expected_effect": "Classify the failure and restore the required checks without weakening them.",
                   "confidence": "verified_ci_failure_not_root_cause", "deterministic_fix_possible": False,
                   "requires_model": True, "requires_founder": True, "dedupe_key": dedupe}
        task = models.Task(title=f"Investigate current-main CI failure: {repository}"[:300],
            description="Classify product/test/infra/capacity/config failure before changing code.",
            objective="Diagnose the evidenced CI failure and prepare the smallest reviewed repair.",
            success_criteria="Record failure category and root cause; reproduce if applicable; project checks pass; independent review; no weakened tests or production changes.",
            project_id=project.id, business_unit_id=project.business_unit_id, assigned_agent_id=agent.id,
            priority=enums.Priority.P1, risk_level=enums.RiskLevel.MEDIUM,
            status=enums.TaskStatus.READY, approval_required=True,
            created_by="repo-guardian", dedupe_key=dedupe,
            context={"autonomous": False, "write": False, "scope": project.key,
                     "guardian": finding, **_external_context(evidence)})
        keys.add_with_key(db, task, "TASK")
        stage_c.advance(db, task, "DETECTED", "repo-guardian",
                        {"repository": repository, "sha": snapshot.main_sha, "risk": "MEDIUM",
                         "expected_effect": finding["expected_effect"]})
        audit.record(db, actor="repo-guardian", action="task.create", entity_type="task",
                     entity_key=task.key, after=finding, task_id=task.id,
                     reason="Verified current-main CI failure; attended diagnosis only")
        db.add(models.Event(event_type="REPOSITORY_GUARDIAN_FINDING", source="repo-guardian",
                            severity=enums.Severity.MEDIUM, project_id=project.id,
                            business_unit_id=project.business_unit_id, task_id=task.id,
                            payload=finding, dedupe_key=dedupe))
        existing_work.append(task)
        out["created"] += 1
        out["tasks"].append(task.key)
        reported[fingerprint] = snapshot.main_sha
    value = {**before, "repository": repository, "last_seen_main_sha": snapshot.main_sha,
             "last_observed_at": snapshot.observed_at.isoformat(),
             "last_review_at": before.get("last_review_at"),
             "open_guardian_task": out["tasks"][0] if out["tasks"] else
                                    (existing_work[0].key if existing_work else None),
             "open_guardian_pr": before.get("open_guardian_pr")}
    # At most the configured bounded check set for this SHA; no unbounded history.
    value["reported_failures"] = {k: v for k, v in reported.items() if v == snapshot.main_sha}
    if snapshot.diff_complete and snapshot.checks_complete and matching and len(matching) == len(snapshot.checks):
        value["last_audited_sha"] = snapshot.main_sha
    if matching and snapshot.checks_complete and all(c.status == "completed" and c.conclusion == "success" for c in matching):
        value["last_successful_ci_sha"] = snapshot.main_sha
    row.value, row.updated_by = value, "repo-guardian"
    if changed:
        db.add(models.Event(event_type="REPOSITORY_MAIN_CHANGED", source="repo-guardian",
                            severity=enums.Severity.INFO, project_id=project.id,
                            payload={"repository": repository, "base_sha": previous,
                                     "head_sha": snapshot.main_sha, "files": snapshot.changed_files,
                                     "diff_complete": snapshot.diff_complete}))
        audit.record(db, actor="repo-guardian", action="repository.checkpoint",
                     entity_type="control_state", entity_key=row.key,
                     before=before, after=value, reason="Observed repository main change")
    db.commit()
    return out
