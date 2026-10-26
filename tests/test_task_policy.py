"""Agent proposals are routed by wording and kind; only read-only work runs on its own."""
import pytest
from sqlalchemy import select

from app import enums, models, registry, task_policy, worker
from tests.test_worker import SPECS, FakeProvider, _db, _good, _task


@pytest.mark.parametrize("title,kind,route", [
    ("Compare competitor onboarding flows", "research", "AUTO"),
    ("Analyse why CTR fell on top pages", "analysis", "AUTO"),
    # Break caught: a model labels a side effect "research" and it runs unattended.
    ("Research and deploy the new pricing page", "research", "APPROVAL"),
    ("Delete stale production rows", "analysis", "FORBIDDEN"),
    ("Draft next quarter plan", "planning", "BACKLOG"),
    ("Fix flaky test in goals", "code_change", "BACKLOG"),
])
def test_routes(monkeypatch, title, kind, route):
    monkeypatch.setenv("PROPOSAL_AUTO_READY", "true")
    assert task_policy.classify(title, "because", kind).route == route


def test_switch_off_keeps_research_in_backlog(monkeypatch):
    monkeypatch.delenv("PROPOSAL_AUTO_READY", raising=False)
    assert task_policy.classify("Compare competitors", "x", "research").route == "BACKLOG"


def test_a_ceo_plan_starts_research_on_its_own_and_parks_the_deploy(monkeypatch):
    monkeypatch.setenv("PROPOSAL_AUTO_READY", "true")
    db = _db()
    parent = _task(db)
    out = _good(parent.key, new_task_candidates=[
        {"title": "Compare competitor onboarding", "reason": "conversion fell", "kind": "research"},
        {"title": "Deploy new landing page", "reason": "test it", "kind": "operations"},
        {"title": "Wipe old analytics", "reason": "cleanup", "kind": "operations"}])
    first = worker.run_once(db, {"claude": FakeProvider("claude", [out])}, SPECS)
    mat = db.get(models.AgentRun, first["run_id"]).tool_calls["materialized"]
    assert len(mat["auto_ready"]) == 1 and len(mat["approval"]) == 1 and len(mat["refused"]) == 1
    research = db.execute(select(models.Task).where(models.Task.key == mat["auto_ready"][0])).scalar_one()
    deploy = db.execute(select(models.Task).where(models.Task.key == mat["approval"][0])).scalar_one()
    assert deploy.status == enums.TaskStatus.BACKLOG and deploy.approval_required
    assert worker._admission_gate(db, research, registry.load()) == (None, SPECS["research"])
    # The next tick runs the research task without anyone touching it; it cannot propose more.
    nxt = _good(research.key, new_task_candidates=[{"title": "More", "reason": "x", "kind": "research"}],
                evidence=[f"token selftest-x and {research.key}"])
    research.context = {**research.context, "verify": {"must_contain": ["selftest-x", research.key]}}
    db.commit()
    second = worker.run_once(db, {"claude": FakeProvider("claude", [nxt]),
                                  "codex": FakeProvider("codex", [nxt])}, SPECS)
    db.refresh(research)
    assert second["task"] == research.key and research.status == enums.TaskStatus.REVIEW
    mat2 = db.get(models.AgentRun, second["run_id"]).tool_calls["materialized"]
    assert mat2["created"] == [] and mat2["dropped"] == 1   # a research child may only hand off code_change
