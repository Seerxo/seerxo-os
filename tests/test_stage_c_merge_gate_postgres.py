"""PostgreSQL serialization for repeated and concurrent Stage C merge receipts."""
from concurrent.futures import ThreadPoolExecutor
import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import audit, enums, models, stage_c, stage_c_ci, stage_c_merge_gate
from tests.test_stage_c_merge_gate import _same_repository_publishing  # noqa: F401 (autouse fixture)

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


@pytest.fixture
def pg():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=5)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    yield engine
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def _ready(engine):
    facts = {"state": "open", "merged": False, "draft": False, "head_sha": SHA,
             "head_ref": "task/TASK-0070", "head_repo": REPO, "base_ref": "main",
             "checks_complete": True, "statuses": dict(GREEN_STATUSES), "statuses_complete": True, "status_problems": [], "checks": OK}
    with Session(engine) as db:
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
        review = audit.record(db, actor="codex", action="improvement.review_approved", entity_type="task",
                              entity_key=task.key, after=approval, task_id=task.id)
        db.flush()
        approval["audit_id"] = review.id
        task.context = {**task.context, "improvement": {**task.context["improvement"], "approval": approval}}
        stage_c.advance(db, task, "PR_OPEN", "stage-c-runner", {
            "branch": "task/TASK-0070", "commit": SHA, "fingerprint": FP,
            "pr": f"https://github.com/{REPO}/pull/7"})
        assert ingest_ci(db, task, facts, repository=REPO,
                                 tracked_branch="main")["outcome"] == "READY_TO_MERGE"
        task.status = enums.TaskStatus.REVIEW
        db.commit()
    return facts


def _task(db, facts):
    return stage_c_merge_gate.task_for_pr(db, 7, facts)


def test_concurrent_merge_authorization_and_completion_are_idempotent(pg):
    facts = _ready(pg)

    def authorize_once():
        with Session(pg) as db:
            task = _task(db, facts)
            return stage_c_merge_gate.authorize(db, task, 7, SHA, facts)

    with ThreadPoolExecutor(max_workers=2) as pool:
        authorizations = list(pool.map(lambda _: authorize_once(), range(2)))
    assert authorizations[0] == authorizations[1]

    merged = {**facts, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
              "merge_commit": MERGE}

    def complete_once():
        with Session(pg) as db:
            task = _task(db, merged)
            return stage_c_merge_gate.complete(db, task, 7, SHA,
                                               authorizations[0]["authorization_id"], merged)

    with ThreadPoolExecutor(max_workers=2) as pool:
        completions = list(pool.map(lambda _: complete_once(), range(2)))
    assert completions[0] == completions[1]
    with Session(pg) as db:
        events = list(db.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "improvement.merge_completed")))
        assert len(events) == 1
        assert events[0].after["authorization_id"] == authorizations[0]["authorization_id"]
