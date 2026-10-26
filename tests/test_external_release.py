"""External evidence admits opted-in work; it never accepts or completes the task."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import audit, control, enums, models, worker
from tests.test_worker import FakeProvider, SPECS, _db, _good, _task

NOW = datetime(2026, 10, 2, 16, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(worker, "ENABLE_AUTOMATIC_EXTERNAL_RELEASE", True, raising=False)
    monkeypatch.setattr(worker, "_now", lambda: NOW)
    session = _db()
    yield session
    session.close()


def waiting(db, **over):
    task = _task(db, status=enums.TaskStatus.WAITING_EXTERNAL, **over)
    task.context = {**task.context, "external_wait": {
        "source": "youtube-auto", "event_type": "YOUTUBE_JOB_TERMINAL",
        "match": {"job": "titanic", "status": ["PUBLISHED", "FAILED"]},
        "after_event_id": 0, "since": (NOW - timedelta(minutes=10)).isoformat(),
    }}
    db.commit()
    return task


def terminal(db, **over):
    event = models.Event(source="youtube-auto", event_type="YOUTUBE_JOB_TERMINAL",
        occurred_at=NOW-timedelta(minutes=1), payload={"job": "titanic", "status": "PUBLISHED",
        "evidence": ["Video ID and published timestamp verified by source adapter"]})
    for key, value in over.items(): setattr(event, key, value)
    db.add(event); db.commit()
    return event


def count(db, kind):
    return db.scalar(select(func.count()).select_from(models.Event).where(models.Event.event_type==kind))


@pytest.mark.parametrize("evidence", [["x" * 2000] * 5, ['"' * 1500] * 3])
def test_oversized_serialized_proof_cannot_release_a_wait(db, evidence):
    task = waiting(db)
    terminal(db, payload={"job": "titanic", "status": "PUBLISHED", "evidence": evidence})
    assert worker.release_external_waits(db, SPECS) == []
    db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    assert "external_release" not in task.context


def test_matching_new_evidence_releases_once_with_audit_and_preserves_attempts(db):
    task = waiting(db, retry_count=1, result="prior analysis")
    event = terminal(db)
    out = worker.run_once(db, {}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY
    assert out["model_calls"] == 0 and task.retry_count == 1 and task.result == "prior analysis"
    assert task.context["external_release"]["event_id"] == event.id
    assert task.context["external_release"]["evidence"] == event.payload["evidence"]
    log = db.scalars(select(models.AuditLog).where(models.AuditLog.action=="task.external_release")).one()
    assert log.before["status"] == "WAITING_EXTERNAL" and log.after["status"] == "READY"
    assert count(db, "EXTERNAL_WAIT_RELEASED") == 1
    worker.run_once(db, {}, SPECS)
    assert count(db, "EXTERNAL_WAIT_RELEASED") == 1


@pytest.mark.parametrize("change", [
    {"source": "other-system"}, {"event_type": "SERVICE_HEALTHY"},
    {"payload": {"job": "other-job", "status": "PUBLISHED", "evidence": ["proof"]}},
    {"payload": {"job": "titanic", "status": "RENDERING", "evidence": ["proof"]}},
    {"payload": {"job": "titanic", "status": "PUBLISHED"}},
    {"payload": {"job": "titanic", "status": "PUBLISHED", "evidence": [""]}},
    {"payload": []},
    {"occurred_at": NOW-timedelta(days=1)}, {"occurred_at": NOW+timedelta(minutes=1)},
])
def test_wrong_stale_future_or_unproven_event_never_wakes_a_model(db, change):
    task = waiting(db); terminal(db, **change)
    fake = FakeProvider("claude", [])
    out = worker.run_once(db, {"claude": fake}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    assert out["model_calls"] == fake.calls == 0


@pytest.mark.parametrize("gate", ["feature_off", "opt_out", "approval", "risk", "write",
    "agent_disabled", "underspecified", "legacy_wait", "malformed_wait"])
def test_release_cannot_bypass_existing_authority_and_eligibility_gates(db, monkeypatch, gate):
    task = waiting(db); terminal(db)
    if gate == "feature_off": monkeypatch.setattr(worker, "ENABLE_AUTOMATIC_EXTERNAL_RELEASE", False)
    elif gate == "opt_out": task.context = {**task.context, "autonomous": False}
    elif gate == "approval": task.approval_required = True
    elif gate == "risk": task.risk_level = enums.RiskLevel.HIGH
    elif gate == "write": task.context = {**task.context, "write": True}
    elif gate == "agent_disabled": db.get(models.Agent, task.assigned_agent_id).enabled = False
    elif gate == "underspecified": task.objective = ""
    elif gate == "legacy_wait": task.context = {"autonomous": True, "scope": "company-os"}
    elif gate == "malformed_wait": task.context = {**task.context, "external_wait": {"source": "youtube-auto"}}
    db.commit(); out = worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL and out["model_calls"] == 0


def test_pause_holds_matching_waits(db):
    task = waiting(db); terminal(db)
    control.set_agents_paused(db, True, actor="founder", reason="maintenance")
    assert worker.run_once(db, {}, SPECS)["paused"] is True
    db.refresh(task); assert task.status == enums.TaskStatus.WAITING_EXTERNAL


def test_dependencies_must_be_done_and_do_not_consume_the_event_while_waiting(db):
    task = waiting(db); event = terminal(db)
    dependency = models.Task(key="TASK-0101", title="accepted spec", success_criteria="founder accepts spec",
        status=enums.TaskStatus.REVIEW)
    db.add(dependency); db.flush()
    db.add(models.TaskDependency(task_id=task.id, depends_on_id=dependency.id)); db.commit()
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    dependency.status = enums.TaskStatus.DONE; db.commit()
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.context["external_release"]["event_id"] == event.id


def test_rearming_requires_an_event_newer_than_the_baseline(db):
    task = waiting(db); event = terminal(db)
    task.context = {**task.context, "external_wait": {**task.context["external_wait"], "after_event_id": event.id}}
    db.commit(); worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    fresh = terminal(db); worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.context["external_release"]["event_id"] == fresh.id


def test_failed_audit_rolls_back_the_release_and_evidence_together(db, monkeypatch):
    task = waiting(db); terminal(db)
    def fail(*args, **kwargs): raise RuntimeError("audit unavailable")
    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"): worker.run_once(db, {}, SPECS)
    db.rollback(); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    assert "external_release" not in task.context and count(db, "EXTERNAL_WAIT_RELEASED") == 0


def test_released_evidence_reaches_the_agent_as_untrusted_data_and_result_stops_at_review(db):
    class CapturingProvider(FakeProvider):
        def _run(self, prompt, schema, **kwargs):
            self.prompt = prompt
            return super()._run(prompt, schema, **kwargs)
    task = waiting(db)
    fake = CapturingProvider("claude", [_good(task.key)])
    assert worker.run_once(db, {"claude": fake}, SPECS)["model_calls"] == 0
    terminal(db); out = worker.run_once(db, {"claude": fake}, SPECS); db.refresh(task)
    assert out["model_calls"] == fake.calls == 1 and task.status == enums.TaskStatus.REVIEW
    assert "Video ID and published timestamp" in fake.prompt
    assert '<external_untrusted encoding="json-string">' in fake.prompt


@pytest.mark.parametrize("change", [
    {"since": "not-a-date"}, {"since": "2026-10-02T15:00:00"},
    {"after_event_id": True}, {"after_event_id": -1}, {"match": {}},
    {"match": {"job": []}}, {"match": {"status": [True]}}, {"unknown": "evaluate me"},
])
def test_malformed_conditions_fail_closed_without_poisoning_the_tick(db, change):
    task = waiting(db); terminal(db)
    task.context = {**task.context, "external_wait": {**task.context["external_wait"], **change}}
    db.commit(); out = worker.run_once(db, {}, SPECS); db.refresh(task)
    assert out["model_calls"] == 0 and task.status == enums.TaskStatus.WAITING_EXTERNAL


def test_project_scope_and_active_project_are_required(db):
    task = waiting(db)
    project = models.Project(key="youtube", name="YouTube", status=enums.ProjectStatus.PAUSED)
    other = models.Project(key="other", name="Other", status=enums.ProjectStatus.ACTIVE)
    db.add_all([project, other]); db.flush()
    task.project_id = project.id; db.commit()
    terminal(db, project_id=project.id)
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    project.status = enums.ProjectStatus.ACTIVE
    # Re-arm after the first result, which belonged to the paused project.
    task.context = {**task.context, "external_wait": {**task.context["external_wait"], "after_event_id": 1}}
    db.commit(); terminal(db, project_id=other.id)
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    correct = terminal(db, project_id=project.id)
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.context["external_release"]["event_id"] == correct.id


def test_business_unit_scope_and_activation_gate_are_preserved(db):
    task = waiting(db)
    unit = models.BusinessUnit(key="youtube", name="YouTube", status=enums.BusinessUnitStatus.PAUSED)
    other = models.BusinessUnit(key="other", name="Other", status=enums.BusinessUnitStatus.ACTIVE)
    db.add_all([unit, other]); db.flush(); task.business_unit_id = unit.id; db.commit()
    terminal(db, business_unit_id=other.id)
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    correct = terminal(db, business_unit_id=unit.id)
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    unit.status = enums.BusinessUnitStatus.ACTIVE; db.commit()
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.context["external_release"]["event_id"] == correct.id


def test_daily_budget_holds_execution_but_not_external_release(db):
    from dataclasses import replace
    task = waiting(db); event = terminal(db)
    specs = {**SPECS, "research": replace(SPECS["research"], budget={"max_runs_per_day": 1})}
    run = models.AgentRun(agent_id=task.assigned_agent_id, task_id=task.id, provider="claude",
        status=enums.AgentRunStatus.SUCCEEDED, started_at=NOW-timedelta(hours=1), ended_at=NOW)
    db.add(run); db.commit()
    fake = FakeProvider("claude", [_good(task.key)])
    out = worker.run_once(db, {"claude": fake}, specs); db.refresh(task)
    assert task.status == enums.TaskStatus.READY
    assert task.context["external_release"]["event_id"] == event.id
    assert out["model_calls"] == fake.calls == 0
    assert worker._eligible(db, task, specs)[0] == "daily_run_budget"
    assert count(db, "EXTERNAL_WAIT_RELEASED") == 1
    run.started_at = NOW-timedelta(days=1); db.commit()
    out = worker.run_once(db, {"claude": fake}, specs); db.refresh(task)
    assert out["model_calls"] == fake.calls == 1 and task.status == enums.TaskStatus.REVIEW
    assert count(db, "EXTERNAL_WAIT_RELEASED") == 1


@pytest.mark.parametrize("exhausted", [False, True])
def test_backoff_and_attempt_limits_hold_execution_not_external_evidence(db, exhausted):
    task = waiting(db, retry_count=1, result="previous result")
    if exhausted: task.retry_count = task.max_attempts
    run = models.AgentRun(agent_id=task.assigned_agent_id, task_id=task.id, provider="claude",
        status=enums.AgentRunStatus.FAILED, started_at=NOW-timedelta(seconds=1), ended_at=NOW)
    db.add(run); db.commit(); event = terminal(db)
    fake = FakeProvider("claude", [])
    out = worker.run_once(db, {"claude": fake}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.READY
    assert task.result == "previous result" and task.retry_count == (task.max_attempts if exhausted else 1)
    assert task.context["external_release"]["event_id"] == event.id
    assert out["model_calls"] == fake.calls == 0
    assert worker._eligible(db, task, SPECS)[0] == ("attempts_exhausted" if exhausted else "backoff")


@pytest.mark.parametrize("cooldown", [False, True])
def test_provider_quota_or_cooldown_does_not_hold_external_state(db, cooldown):
    from app.providers import Health
    task = waiting(db); event = terminal(db)
    providers = {name: FakeProvider(name, [], health=Health.QUOTA_EXHAUSTED)
                 for name in ("claude", "codex")}
    if cooldown:
        worker._cool(db, [{"provider": name, "health": Health.QUOTA_EXHAUSTED.value}
                          for name in providers])
    out = worker.run_once(db, providers, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.retry_count == 0
    assert task.context["external_release"]["event_id"] == event.id
    assert out["model_calls"] == sum(p.calls for p in providers.values()) == 0


def test_disabling_release_preserves_prior_proof_and_stops_new_admission(db, monkeypatch):
    released = waiting(db); terminal(db)
    assert worker.release_external_waits(db, SPECS) == [released.key]
    proof = released.context["external_release"]
    held = waiting(db, key="TASK-0101")
    monkeypatch.setattr(worker, "ENABLE_AUTOMATIC_EXTERNAL_RELEASE", False)
    assert worker.release_external_waits(db, SPECS) == []
    db.refresh(released); db.refresh(held)
    assert released.status == enums.TaskStatus.READY and released.context["external_release"] == proof
    assert held.status == enums.TaskStatus.WAITING_EXTERNAL and count(db, "EXTERNAL_WAIT_RELEASED") == 1


def test_rearm_does_not_replay_the_previously_released_event_even_with_old_cursor(db):
    task = waiting(db); terminal(db); worker.run_once(db, {}, SPECS)
    db.refresh(task); assert task.status == enums.TaskStatus.READY
    task.status = enums.TaskStatus.WAITING_EXTERNAL; db.commit()
    worker.run_once(db, {}, SPECS); db.refresh(task)
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL
    assert count(db, "EXTERNAL_WAIT_RELEASED") == 1


@pytest.mark.parametrize("status", [enums.TaskStatus.WAITING_APPROVAL, enums.TaskStatus.BLOCKED,
    enums.TaskStatus.DONE, enums.TaskStatus.CANCELLED, enums.TaskStatus.REVIEW])
def test_relaxed_dispatch_check_cannot_admit_other_non_ready_states(db, status):
    from app import dispatch
    task = _task(db, status=status)
    agent = db.get(models.Agent, task.assigned_agent_id)
    assert dispatch.skip_reason(task, agent, None, [], require_ready=False) == "task_not_ready"
