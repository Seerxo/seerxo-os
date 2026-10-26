"""A delivered PR is observable after the write attempt lost both responses."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session

from app import audit, control, enums, models, stage_c_ci, stage_c_runner, stage_c_worker, worker
from tests.test_stage_c_worker import Reviewer, SPECS, _db, pipeline, record_coders, write_task


@pytest.fixture
def failed_publication(monkeypatch, tmp_path, request):
    from app import repo_guardian_github
    destination = getattr(request, "param", "seerxo/seerxo-os")
    monkeypatch.setattr(repo_guardian_github, "publish_repository", lambda policy, repository: destination)
    def lost_response(*args):
        raise TimeoutError("PR response and immediate verification unavailable")

    remote = pipeline(monkeypatch, tmp_path, {"during_open_pr": lost_response}, publish_repository=destination)
    git = stage_c_runner._git
    def local_git(repo, *args, **kwargs):
        if args[:2] == ("ls-remote", f"https://github.com/{destination}.git"):
            args = (args[0], str(remote), *args[2:])
            kwargs["protocol"] = "file"
        return git(repo, *args, **kwargs)
    monkeypatch.setattr(stage_c_runner, "_git", local_git)
    coded = record_coders(monkeypatch)
    db = _db()
    task = write_task(db)
    providers = {name: Reviewer(name) for name in ("claude", "codex")}
    result = worker.run_once(db, providers, SPECS)
    assert result["verdict"] == "PR_FAILED" and task.status == enums.TaskStatus.BLOCKED
    run = db.query(models.AgentRun).one()
    reads = []
    url = "https://github.com/seerxo/seerxo-os/pull/999"

    def lookup(branch, commit):
        reads.append((branch, commit))
        return url

    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup", lambda *args: lookup)
    monkeypatch.setattr(worker, "ENABLE_AUTONOMOUS_CODE_WRITE", False)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *args, **kwargs: {
        "state": "open", "merged": False, "head_sha": task.artifacts["commit"],
        "checks_complete": False, "checks": [], "statuses": {},
        "statuses_complete": True, "status_problems": []})
    return db, task, run, coded, providers, reads, remote


def test_transient_pr_lookup_failure_recovers_delivery_without_another_execution(failed_publication):
    db, task, run, coded, providers, reads, _ = failed_publication
    stage_c_worker.reconcile(db)

    assert task.status == enums.TaskStatus.REVIEW
    assert task.context["improvement"]["stage"] == "PR_OPEN"
    assert task.artifacts["pr"] == "https://github.com/seerxo/seerxo-os/pull/999" and len(reads) == 1
    assert coded == ["claude"] and len(providers["codex"].packets) == 1
    assert db.query(models.AgentRun).count() == 1 and run.status == enums.AgentRunStatus.FAILED
    assert db.query(models.AuditLog).filter_by(action="improvement.publication_recovered").count() == 1
    stage_c_worker.reconcile(db)
    assert len(reads) == 1 and db.query(models.AgentRun).count() == 1


@pytest.mark.parametrize("failed_publication", ["seerxo-agent/seerxo-os"], indirect=True)
def test_b5_fork_publication_recovers_after_restart_without_another_coder_or_pr_create(failed_publication, monkeypatch):
    db, task, run, coded, providers, reads, _ = failed_publication
    task_id = task.id
    # Close the session: only the database receipt and existing branch survive the restart.
    engine = db.get_bind()
    db.close()
    fresh = Session(engine)
    lookups = []

    def lookup(repository, branch, head_repository):
        lookups.append((repository, branch, head_repository))
        return lambda ref, sha: reads.append((ref, sha)) or "https://github.com/seerxo/seerxo-os/pull/999"

    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup", lookup)
    monkeypatch.setattr(stage_c_runner, "gh_pr_opener", lambda *a: pytest.fail("recovery must not create a PR"))
    stage_c_worker.reconcile(fresh)
    task = fresh.get(models.Task, task_id)
    assert task.status == enums.TaskStatus.REVIEW and task.context["improvement"]["stage"] == "PR_OPEN"
    assert lookups == [("seerxo/seerxo-os", "main", "seerxo-agent/seerxo-os")]
    assert len(reads) == 1 and coded == ["claude"] and len(providers["codex"].packets) == 1
    assert fresh.query(models.AgentRun).count() == 1
    stage_c_worker.reconcile(fresh)
    assert len(reads) == 1 and fresh.query(models.AgentRun).count() == 1
    fresh.close()


@pytest.mark.parametrize("change", ["founder_block", "keyed_founder_block", "owner", "contract", "paused_project", "read_only",
                                     "run_id", "commit", "receipt", "review", "run", "legacy", "task_key"])
def test_unclear_or_changed_authority_never_reopens_a_block(failed_publication, change):
    db, task, run, _, _, reads, _ = failed_publication
    if change in ("founder_block", "keyed_founder_block"):
        audit.record(db, actor="founder", action="task.status", entity_type="task", entity_key=task.key,
                     after={"status": "BLOCKED"}, task_id=task.id if change == "founder_block" else None)
    elif change == "owner":
        task.assigned_agent_id = db.query(models.Agent).filter_by(key="ceo").one().id
    elif change == "contract":
        task.objective = "different work"
    elif change == "paused_project":
        db.get(models.Project, task.project_id).status = enums.ProjectStatus.PAUSED
    elif change == "read_only":
        task.context = {**task.context, "write": False}
    elif change == "run_id":
        task.artifacts = {**task.artifacts, "run_id": True}
        flag_modified(task, "artifacts")  # True == 1 otherwise suppresses this corrupt JSON write.
    elif change == "commit":
        task.artifacts = {**task.artifacts, "commit": "a" * 40}
    elif change == "receipt":
        run.tool_calls = {**run.tool_calls, "publication": {}}
    elif change == "review":
        approval = task.context["improvement"]["approval"]
        db.get(models.AuditLog, approval["audit_id"]).actor = "claude"
    elif change == "run":
        run.status = enums.AgentRunStatus.SUCCEEDED
    elif change == "legacy":
        run.tool_calls = {key: value for key, value in run.tool_calls.items() if key != "publication"}
    else:
        task.key = "../TASK-0100"
    db.commit()
    stage_c_worker.reconcile(db)
    assert task.status == enums.TaskStatus.BLOCKED and reads == []
    assert db.query(models.AgentRun).count() == 1


@pytest.mark.parametrize("checkpoint", [None, {}, "bad", {"attempts": True, "next_check_at": "now"},
    {"attempts": 0, "next_check_at": "2026-10-01T00:00:00+00:00"},
    {"attempts": 1, "next_check_at": "2026-10-01T00:00:00"}])
def test_malformed_recovery_checkpoint_does_not_reset_the_probe_budget(failed_publication, checkpoint):
    db, task, run, _, _, reads, _ = failed_publication
    run.tool_calls = {**run.tool_calls, "publication_recovery": checkpoint}
    db.commit()
    stage_c_worker.reconcile(db)
    assert task.status == enums.TaskStatus.BLOCKED and reads == []
    assert run.tool_calls["publication_recovery"] == checkpoint
    assert "checkpoint" in task.error and "founder" in task.error


def test_crash_after_the_final_reserved_probe_does_not_strand_an_unexplained_block(failed_publication):
    db, task, run, _, _, reads, _ = failed_publication
    run.tool_calls = {**run.tool_calls, "publication_recovery": {
        "attempts": 3, "next_check_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()}}
    db.commit()
    stage_c_worker.reconcile(db)
    assert reads == [] and task.status == enums.TaskStatus.BLOCKED
    assert "exhausted" in task.error and "founder" in task.error


def test_failed_observation_has_durable_cooldown_and_three_probe_limit(failed_publication, monkeypatch):
    db, task, run, _, _, _, _ = failed_publication
    seen, clock = [], [datetime.now(timezone.utc)]
    monkeypatch.setattr(worker, "_now", lambda: clock[0])
    def unavailable(*args):
        seen.append(1)
        raise RuntimeError("private token response must not be persisted")
    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup", lambda *args: unavailable)
    for attempt in range(3):
        stage_c_worker.reconcile(db)
        assert len(seen) == attempt + 1
        assert run.tool_calls["publication_recovery"]["attempts"] == attempt + 1
        stage_c_worker.reconcile(db)
        assert len(seen) == attempt + 1
        clock[0] += timedelta(minutes=6)
    for _ in range(3):
        stage_c_worker.reconcile(db)
    assert seen == [1, 1, 1] and "founder" in task.error
    assert "private" not in task.error and db.query(models.AgentRun).count() == 1


def test_founder_takeover_during_the_read_wins(failed_publication, monkeypatch):
    db, task, _, _, _, reads, _ = failed_publication
    def takeover(*args):
        reads.append(1)
        task.error = "founder decision: keep blocked"
        audit.record(db, actor="founder", action="task.status", entity_type="task", entity_key=task.key,
                     after={"status": "BLOCKED"}, task_id=task.id)
        db.commit()
        return "https://github.com/seerxo/seerxo-os/pull/999"
    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup", lambda *args: takeover)
    stage_c_worker.reconcile(db)
    assert task.status == enums.TaskStatus.BLOCKED and task.error == "founder decision: keep blocked"
    assert len(reads) == 1


@pytest.mark.parametrize("change", ["project", "review", "receipt", "paused"])
def test_concurrent_restriction_is_visible_with_the_production_session_cache(failed_publication, monkeypatch, change):
    db, task, run, _, _, reads, _ = failed_publication
    db.expire_on_commit = False
    project = db.get(models.Project, task.project_id)
    review = db.get(models.AuditLog, task.context["improvement"]["approval"]["audit_id"])
    changed = []
    def changed_during_lookup(*args):
        reads.append(1)
        with Session(db.get_bind()) as other:
            if change == "project":
                other.get(models.Project, project.id).status = enums.ProjectStatus.PAUSED
            elif change == "review":
                other.get(models.AuditLog, review.id).actor = "claude"
            elif change == "receipt":
                other.get(models.AgentRun, run.id).tool_calls = {"stage_c": True}
            else:
                control.set_agents_paused(other, True, actor="founder", reason="stop")
            other.commit()
        changed.append(True)
        return "https://github.com/seerxo/seerxo-os/pull/999"
    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup", lambda *args: changed_during_lookup)
    stage_c_worker.reconcile(db)
    assert changed == [True] and task.status == enums.TaskStatus.BLOCKED and reads == [1]


def test_a_second_observer_cannot_spend_the_reserved_probe(failed_publication, monkeypatch):
    db, task, run, _, _, reads, _ = failed_publication
    def concurrent_lookup(*args):
        reads.append(1)
        with Session(db.get_bind()) as other:
            assert stage_c_worker.reconcile(other) == []
        return "https://github.com/seerxo/seerxo-os/pull/999"
    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup", lambda *args: concurrent_lookup)
    stage_c_worker.reconcile(db)
    assert task.status == enums.TaskStatus.REVIEW and reads == [1]
    assert run.tool_calls["publication_recovery"]["attempts"] == 1


def test_an_absent_remote_head_never_becomes_a_recorded_delivery(failed_publication):
    db, task, _, _, _, reads, remote = failed_publication
    stage_c_runner._git(remote, "update-ref", "-d", f"refs/heads/task/{task.key}")
    stage_c_worker.reconcile(db)
    assert task.status == enums.TaskStatus.BLOCKED and reads == []
    assert "pr" not in task.artifacts
