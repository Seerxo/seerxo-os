"""Audited pre-merge authorization for the Stage C founder merge command."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import re
from pathlib import Path

from sqlalchemy import select

from app import audit, enums, models, repo_guardian_github, stage_c_ci

REPOSITORY = "seerxo/seerxo-os"
FOUNDER = "semihbugrasezer"
ACTOR = "scripts/merge-pr"
TASK_BRANCH = re.compile(r"^task/(TASK-[0-9]{1,6})$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def _task_for_pr_number(db, number: int) -> models.Task | None:
    matches = []
    for task in db.scalars(select(models.Task).where(models.Task.key.like("TASK-%"))):
        published = stage_c_ci._evidence(task, "PR_OPEN")
        if published.get("pr") == f"https://github.com/{REPOSITORY}/pull/{number}":
            matches.append(task)
    if len(matches) > 1:
        raise ValueError("PR number is published by multiple Stage C tasks")
    return matches[0] if matches else None


def identify_pr(db, number: int) -> dict:
    """Classify by durable publication evidence, so renaming a task branch cannot bypass the gate."""
    task = _task_for_pr_number(db, number)
    return {"stage_c": task is not None, "pr_number": number,
            **({"task_key": task.key} if task else {})}


def _policy() -> dict:
    return repo_guardian_github.load_policy(Path(__file__).resolve().parents[1] / "agents/repository_guardian.yaml")


def head_repository(policy: dict | None = None) -> str:
    """Stage C PR heads come from exactly this repository: the configured publish fork, else REPOSITORY."""
    return repo_guardian_github.publish_repository(policy or _policy(), REPOSITORY)


def task_for_pr(db, number: int, facts: dict) -> models.Task:
    if facts.get("head_repo") != head_repository():
        raise ValueError("PR head repository does not match the authorized repository")
    task = _task_for_pr_number(db, number)
    if not task:
        raise ValueError("PR number is not published by a Stage C task")
    task = db.scalar(select(models.Task).where(models.Task.id == task.id).with_for_update()
                     .execution_options(populate_existing=True))
    if not task:
        raise ValueError("Stage C task disappeared during merge authorization")
    published = stage_c_ci._evidence(task, "PR_OPEN")
    if (published.get("pr") != f"https://github.com/{REPOSITORY}/pull/{number}"
            or facts.get("head_ref") != published.get("branch")
            or not TASK_BRANCH.fullmatch(published.get("branch", ""))):
        raise ValueError("PR branch or publication does not match the Stage C task identity")
    return task


def authorize(db, task: models.Task, number: int, head: str, facts: dict) -> dict:
    if facts.get("head_sha") != head:
        raise ValueError("PR head changed before authorization")
    approval = ((task.context or {}).get("improvement") or {}).get("approval") or {}
    after = {"repository": REPOSITORY, "pr_number": number, "head_sha": head,
             "fingerprint": approval.get("fingerprint"), "review_audit_id": approval.get("audit_id"),
             "checks": facts["checks"]}
    existing = list(db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "improvement.merge_authorized",
        models.AuditLog.entity_key == task.key).with_for_update()))
    prior = next((event for event in reversed(existing) if (
        event.entity_type == "task" and event.task_id == task.id and event.actor == ACTOR
        and event.after == after)), None)
    if facts.get("merged"):
        if prior is None:
            raise ValueError("merged PR has no prior Stage C merge authorization")
        db.commit()
        return {"authorization_id": prior.id, **after}

    if task.status != enums.TaskStatus.REVIEW:
        raise ValueError("merge refused: Stage C task is not in REVIEW state")
    policy = _policy()
    branch = repo_guardian_github.tracked_branch(policy, REPOSITORY)
    _, contexts = repo_guardian_github.merge_statuses(policy)
    decision = stage_c_ci._eligibility(db, task, task.context or {}, facts, REPOSITORY, branch, contexts,
                                       head_repository=head_repository(policy))
    if not decision.eligible:
        raise ValueError("merge refused: " + "; ".join(decision.reasons or ["task is not merge eligible"]))
    if prior is not None:
        db.commit()
        return {"authorization_id": prior.id, **after}

    event = audit.record(db, actor=ACTOR, action="improvement.merge_authorized", entity_type="task",
                         entity_key=task.key, reason="exact-head CI and independent review verified",
                         after=after, task_id=task.id)
    db.flush()
    receipt = {"authorization_id": event.id, **after}
    db.commit()
    return receipt


def complete(db, task: models.Task, number: int, head: str, authorization_id: int,
             facts: dict) -> dict:
    authorization = db.scalar(select(models.AuditLog).where(
        models.AuditLog.id == authorization_id).with_for_update())
    if (not authorization or authorization.action != "improvement.merge_authorized"
            or authorization.entity_type != "task" or authorization.entity_key != task.key
            or authorization.task_id != task.id
            or authorization.actor != ACTOR):
        raise ValueError("merge authorization audit record is missing or mismatched")
    evidence = authorization.after or {}
    approval = ((task.context or {}).get("improvement") or {}).get("approval") or {}
    if (evidence.get("repository") != REPOSITORY or evidence.get("pr_number") != number
            or evidence.get("fingerprint") != approval.get("fingerprint")
            or evidence.get("review_audit_id") != approval.get("audit_id")
            or evidence.get("head_sha") != head or facts.get("head_sha") != head
            or facts.get("head_repo") != head_repository() or facts.get("base_ref") != "main"
            or not facts.get("merged") or facts.get("merged_by") != FOUNDER
            or facts.get("draft") or not SHA.fullmatch(facts.get("merge_commit") or "")):
        raise ValueError("GitHub does not prove the authorized founder merge")

    policy = _policy()
    branch = repo_guardian_github.tracked_branch(policy, REPOSITORY)
    _, contexts = repo_guardian_github.merge_statuses(policy)
    premerge = {**facts, "state": "open", "merged": False}
    context = deepcopy(task.context or {})
    context.setdefault("improvement", {})["stage"] = "READY_TO_MERGE"
    decision = stage_c_ci._eligibility(db, task, context, premerge, REPOSITORY, branch, contexts,
                                       head_repository=head_repository(policy))
    if not decision.eligible:
        raise ValueError("merge no longer matches its authorization: " + "; ".join(decision.reasons))

    after = {"authorization_id": authorization_id, "repository": REPOSITORY,
             "pr_number": number, "head_sha": head, "merge_commit": facts["merge_commit"],
             "merged_by": facts["merged_by"], "review_audit_id": evidence["review_audit_id"]}
    events = list(db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "improvement.merge_completed",
        models.AuditLog.entity_key == task.key).with_for_update()))
    for event in events:
        if (event.after or {}).get("authorization_id") == authorization_id:
            if (event.entity_type != "task" or event.task_id != task.id or event.actor != ACTOR):
                raise ValueError("completion audit record identity mismatch")
            if event.after != after:
                raise ValueError("conflicting completion already recorded for this authorization")
            db.commit()
            return {"completed": True, "audit_id": event.id, **after}

    event = audit.record(db, actor=ACTOR, action="improvement.merge_completed", entity_type="task",
                         entity_key=task.key, reason="GitHub merge commit and actor verified",
                         before={"authorization_id": authorization_id}, after=after, task_id=task.id)
    db.flush()
    receipt = {"completed": True, "audit_id": event.id, **after}
    db.commit()
    return receipt


def _github_facts(number: int) -> dict:
    policy = _policy()
    publisher, contexts = repo_guardian_github.merge_statuses(policy)
    return stage_c_ci.gh_facts(REPOSITORY, number, set(policy["workflows"]),
                               publisher=publisher, status_contexts=contexts)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("identify", "authorize", "complete"):
        command = sub.add_parser(action)
        command.add_argument("--pr", type=int, required=True)
        if action != "identify":
            command.add_argument("--head", required=True)
    sub.choices["complete"].add_argument("--authorization-id", type=int, required=True)
    args = parser.parse_args()
    if args.pr <= 0:
        raise SystemExit("invalid PR number")
    if args.action != "identify" and not SHA.fullmatch(args.head):
        raise SystemExit("invalid head SHA")

    from app.db import SessionLocal
    with SessionLocal() as db:
        if args.action == "identify":
            result = identify_pr(db, args.pr)
        else:
            facts = _github_facts(args.pr)
            task = task_for_pr(db, args.pr, facts)
        if args.action == "authorize":
            result = authorize(db, task, args.pr, args.head, facts)
        elif args.action == "complete":
            result = complete(db, task, args.pr, args.head, args.authorization_id, facts)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, TypeError):
        raise SystemExit("Stage C merge gate refused or evidence unavailable") from None
