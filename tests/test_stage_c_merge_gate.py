import copy

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import audit, enums, models, stage_c, stage_c_ci, stage_c_merge_gate


@pytest.fixture(autouse=True)
def _same_repository_publishing(monkeypatch):
    """These tests cover publishing from Seerxo/os itself; the repository's policy now names the B5 fork, whose
    path the B5 tests cover by setting it explicitly."""
    from app import repo_guardian_github
    load = repo_guardian_github.load_policy
    monkeypatch.setattr(repo_guardian_github, "load_policy",
                        lambda path: {k: v for k, v in load(path).items() if k != "publish_forks"})

REQUIRED_CONTEXTS = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")
GREEN_STATUSES = {context: "success" for context in REQUIRED_CONTEXTS}


def ingest_ci(*args, **kwargs):
    """ingest with the policy's required commit statuses (the production contract)."""
    return stage_c_ci.ingest(*args, required_statuses=REQUIRED_CONTEXTS, **kwargs)


REPO = stage_c_merge_gate.REPOSITORY
SHA = "a" * 40
FP = "f" * 64
MERGE = "b" * 40
OK = [{"name": "test", "status": "completed", "conclusion": "success"}]


def _ready():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    task = models.Task(key="TASK-0070", title="fix", success_criteria="reviewed CI",
                       priority=enums.Priority.P1)
    db.add(task)
    db.flush()
    for state in ("DETECTED", "PLANNED", "IMPLEMENTING"):
        stage_c.advance(db, task, state, "stage-c-runner")
    stage_c.advance(db, task, "TESTING", "stage-c-runner", {"coder": "claude", "fingerprint": FP})
    stage_c.advance(db, task, "INDEPENDENT_REVIEW", "stage-c-runner", {"fingerprint": FP})
    review_at = task.context["improvement"]["history"][-1]["at"]
    approval = {"fingerprint": FP, "review_stage_at": review_at, "reviewer": "codex", "coder": "claude"}
    event = audit.record(db, actor="codex", action="improvement.review_approved", entity_type="task",
                         entity_key=task.key, after=approval, task_id=task.id)
    db.flush()
    approval["audit_id"] = event.id
    task.context = {**task.context, "improvement": {**task.context["improvement"], "approval": approval}}
    stage_c.advance(db, task, "PR_OPEN", "stage-c-runner", {
        "branch": "task/TASK-0070", "commit": SHA, "fingerprint": FP,
        "pr": f"https://github.com/{REPO}/pull/7"})
    facts = {"state": "open", "merged": False, "draft": False, "head_sha": SHA,
             "head_ref": "task/TASK-0070", "head_repo": REPO, "base_ref": "main",
             "checks_complete": True, "statuses": dict(GREEN_STATUSES), "statuses_complete": True, "status_problems": [], "checks": OK}
    assert ingest_ci(db, task, facts, repository=REPO,
                             tracked_branch="main")["outcome"] == "READY_TO_MERGE"
    task.status = enums.TaskStatus.REVIEW
    return db, task, facts


def test_authorization_and_verified_merge_are_audited_and_idempotent():
    db, task, facts = _ready()

    first = stage_c_merge_gate.authorize(db, task, 7, SHA, facts)
    second = stage_c_merge_gate.authorize(db, task, 7, SHA, facts)
    assert first == second
    auth = db.get(models.AuditLog, first["authorization_id"])
    assert auth.action == "improvement.merge_authorized"
    assert auth.after["review_audit_id"] == task.context["improvement"]["approval"]["audit_id"]

    merged = {**facts, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
              "merge_commit": MERGE}
    result = stage_c_merge_gate.complete(db, task, 7, SHA, first["authorization_id"], merged)
    again = stage_c_merge_gate.complete(db, task, 7, SHA, first["authorization_id"], merged)
    assert result == again and result["completed"] is True
    done = db.get(models.AuditLog, result["audit_id"])
    assert done.action == "improvement.merge_completed"
    assert done.after["authorization_id"] == auth.id
    assert done.after["merge_commit"] == MERGE and done.after["merged_by"] == stage_c_merge_gate.FOUNDER


def test_pr_classification_uses_publication_identity_instead_of_branch_name():
    db, task, facts = _ready()
    assert stage_c_merge_gate.identify_pr(db, 7) == {"stage_c": True, "pr_number": 7, "task_key": task.key}
    assert stage_c_merge_gate.identify_pr(db, 8) == {"stage_c": False, "pr_number": 8}
    renamed = {**facts, "head_ref": "feat/renamed"}

    with pytest.raises(ValueError, match="branch or publication"):
        stage_c_merge_gate.task_for_pr(db, 7, renamed)


def test_pr_authorization_fails_closed_if_task_disappears_after_classification(monkeypatch):
    db, _, facts = _ready()
    monkeypatch.setattr(db, "scalar", lambda statement: None)

    with pytest.raises(ValueError, match="task disappeared"):
        stage_c_merge_gate.task_for_pr(db, 7, facts)


def test_authorization_does_not_reuse_audit_row_with_a_forged_actor():
    db, task, facts = _ready()
    forged = stage_c_merge_gate.authorize(db, task, 7, SHA, facts)
    db.get(models.AuditLog, forged["authorization_id"]).actor = "forged-actor"

    receipt = stage_c_merge_gate.authorize(db, task, 7, SHA, facts)

    assert receipt["authorization_id"] != forged["authorization_id"]
    assert db.get(models.AuditLog, receipt["authorization_id"]).actor == stage_c_merge_gate.ACTOR


def test_completion_retry_rejects_a_replayed_audit_row_with_wrong_identity():
    db, task, facts = _ready()
    receipt = stage_c_merge_gate.authorize(db, task, 7, SHA, facts)
    merged = {**facts, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
              "merge_commit": MERGE}
    done = stage_c_merge_gate.complete(db, task, 7, SHA, receipt["authorization_id"], merged)
    db.get(models.AuditLog, done["audit_id"]).actor = "forged-actor"

    with pytest.raises(ValueError, match="identity mismatch"):
        stage_c_merge_gate.complete(db, task, 7, SHA, receipt["authorization_id"], merged)


@pytest.mark.parametrize("changes", [
    {"merged_by": "some-bot"},
    {"head_sha": "c" * 40},
    {"head_repo": "attacker/fork"},
    {"merge_commit": None},
])
def test_unverified_github_merge_facts_never_create_completion_audit(changes):
    db, task, facts = _ready()
    receipt = stage_c_merge_gate.authorize(db, task, 7, SHA, facts)
    merged = {**facts, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
              "merge_commit": MERGE, **changes}

    with pytest.raises(ValueError):
        stage_c_merge_gate.complete(db, task, 7, SHA, receipt["authorization_id"], merged)

    assert db.scalar(select(models.AuditLog).where(
        models.AuditLog.action == "improvement.merge_completed")) is None


def test_authorization_refuses_unreviewed_or_moved_head():
    db, task, facts = _ready()
    stale_facts = {**facts, "head_sha": "c" * 40}
    with pytest.raises(ValueError, match="head changed"):
        stage_c_merge_gate.authorize(db, task, 7, SHA, stale_facts)

    task.context = copy.deepcopy(task.context)
    task.context["improvement"].pop("approval")
    with pytest.raises(ValueError, match="merge refused"):
        stage_c_merge_gate.authorize(db, task, 7, SHA, facts)


@pytest.mark.parametrize("status", [enums.TaskStatus.DONE, enums.TaskStatus.CANCELLED])
def test_terminal_task_status_cannot_receive_new_merge_authorization(status):
    db, task, facts = _ready()
    task.status = status
    with pytest.raises(ValueError, match="not in REVIEW state"):
        stage_c_merge_gate.authorize(db, task, 7, SHA, facts)


def test_retry_recovers_only_an_existing_authorization_after_github_merge():
    db, task, facts = _ready()
    authorized = stage_c_merge_gate.authorize(db, task, 7, SHA, facts)
    merged = {**facts, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
              "merge_commit": MERGE}
    recovered = stage_c_merge_gate.authorize(db, task, 7, SHA, merged)
    assert recovered == authorized
    completed = stage_c_merge_gate.complete(db, task, 7, SHA, recovered["authorization_id"], merged)
    assert completed["completed"] is True


def test_already_merged_pr_without_prior_authorization_cannot_be_recovered():
    db, task, facts = _ready()
    merged = {**facts, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
              "merge_commit": MERGE}
    with pytest.raises(ValueError, match="no prior Stage C merge authorization"):
        stage_c_merge_gate.authorize(db, task, 7, SHA, merged)


FORK = "seerxo-agent/os"


def test_b5_fork_pr_is_authorized_and_completed_only_from_the_configured_fork(monkeypatch):
    policy = stage_c_merge_gate._policy()
    monkeypatch.setattr(stage_c_merge_gate, "_policy", lambda: {**policy, "publish_forks": {REPO: FORK}})
    db, task, facts = _ready()
    facts = {**facts, "head_repo": FORK}
    receipt = stage_c_merge_gate.authorize(db, stage_c_merge_gate.task_for_pr(db, 7, facts), 7, SHA, facts)
    merged = {**facts, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
              "merge_commit": MERGE}
    assert stage_c_merge_gate.complete(db, task, 7, SHA, receipt["authorization_id"], merged)["completed"]
    for head_repo in (REPO, "attacker/os"):
        with pytest.raises(ValueError, match="head repository"):
            stage_c_merge_gate.task_for_pr(db, 7, {**facts, "head_repo": head_repo})
