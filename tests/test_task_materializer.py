"""Approved runs' task candidates become bounded BACKLOG tasks; nothing fans out."""
import pytest
from sqlalchemy import select

from app import desk, enums, models, repo_guardian, worker
from tests.test_worker import SPECS, FakeProvider, _db, _good, _task


def _cands(n):
    return [{"title": f"Investigate churn source {i}", "reason": f"MRR fell; check cohort {i}"}
            for i in range(n)]


def _run(db, task, cands, verdict_ok=True):
    out = _good(task.key, new_task_candidates=cands)
    if not verdict_ok:
        out["evidence"] = []  # supervisor rejects: no verification token
    return worker.run_once(db, {"claude": FakeProvider("claude", [out])}, SPECS)


def _children(db):
    return list(db.execute(select(models.Task).where(
        models.Task.created_by == "task-materializer").order_by(models.Task.id)).scalars())


def test_an_approved_run_creates_at_most_three_backlog_tasks_with_the_parents_scope():
    db = _db()
    goal = models.Goal(title="MRR $100", metric="mrr", target="100", status=enums.GoalStatus.ACTIVE)
    db.add(goal)
    db.commit()
    parent = _task(db, goal_id=goal.id, priority=enums.Priority.P1)
    out = _run(db, parent, _cands(5))
    kids = _children(db)
    # Break caught: a model's five ideas become five tasks (or zero, as before).
    assert len(kids) == 3
    run = db.get(models.AgentRun, out["run_id"])
    assert run.tool_calls["materialized"]["dropped"] == 2
    k = kids[0]
    assert k.status == enums.TaskStatus.BACKLOG and k.assigned_agent_id is None
    assert k.goal_id == goal.id and k.priority == enums.Priority.P1
    assert {key: k.context[key] for key in ("parent", "origin_run", "depth", "reason", "autonomous",
                                            "policy")} == {
        "parent": parent.key, "origin_run": run.id, "depth": 1,
        "reason": "MRR fell; check cohort 0", "autonomous": False, "policy": "BACKLOG"}
    assert desk.origin(k)["trigger"] == f"proposed by {parent.key} (run {run.id})"


def test_a_proposed_task_cannot_propose_more():
    db = _db()
    child = _task(db, context={"autonomous": True, "scope": "company-os", "depth": 1,
                               "verify": {"must_contain": ["selftest-x", "TASK-0100"]}})
    out = _run(db, child, _cands(2))
    assert out["verdict"] == "APPROVED", out
    # Break caught: agent -> 3 tasks -> 9 -> 27.
    assert _children(db) == []
    assert "depth limit" in db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]["reason"]


def test_rejected_runs_cannot_create_proposals_even_when_called_directly():
    db = _db()
    parent = _task(db)
    _run(db, parent, _cands(1), verdict_ok=False)
    assert _children(db) == []
    from app import task_materializer
    run = db.execute(select(models.AgentRun)).scalars().first()
    cands = [worker.TaskCandidate(**c) for c in _cands(1)]
    task_materializer.materialize(db, parent, run, cands)
    task_materializer.materialize(db, parent, run, cands)
    db.commit()
    assert _children(db) == []


def test_approved_proposals_are_not_duplicated_when_materialization_is_repeated():
    from app import task_materializer

    db = _db()
    parent = _task(db)
    _run(db, parent, _cands(1))
    run = db.scalar(select(models.AgentRun))
    task_materializer.materialize(db, parent, run, [worker.TaskCandidate(**c) for c in _cands(1)])
    db.commit()
    assert len(_children(db)) == 1


@pytest.mark.parametrize("change", ["status", "verdict", "task", "agent"])
def test_materialization_requires_the_approved_completion_for_this_parent_and_owner(change):
    from app import task_materializer

    db = _db()
    parent = _task(db)
    run = models.AgentRun(task_id=parent.id, agent_id=parent.assigned_agent_id, provider="claude",
                          status=enums.AgentRunStatus.SUCCEEDED,
                          supervisor_verdict=enums.SupervisorVerdict.APPROVED)
    db.add(run)
    db.commit()
    if change == "status":
        run.status = enums.AgentRunStatus.STARTED
    elif change == "verdict":
        run.supervisor_verdict = enums.SupervisorVerdict.ESCALATE
    elif change == "task":
        run.task_id = _task(db, key="TASK-0101").id
    else:
        run.agent_id = db.scalar(select(models.Agent.id).where(models.Agent.key == "ceo"))
    outcome = task_materializer.materialize(db, parent, run, [worker.TaskCandidate(**c) for c in _cands(1)])
    db.commit()
    assert _children(db) == [] and outcome["reason"] == "run_not_approved_for_parent"


# --- Research -> Developer handoff (code_change proposals) -------------------------------------------------
FOUNDER_ID = "founder:access:founder@example.invalid"
SHA = "a" * 40
METRICS = "tests/test_metrics.py"


REPO = "https://github.com/seerxo/seerxo-os"


def _project(db, repository=REPO, status=enums.ProjectStatus.ACTIVE):
    # Stage C hand-offs are now bound to the canonical project/repository profile.
    canonical = repo_guardian.repository_name(repository)
    key = "company-os" if canonical == "seerxo/seerxo-os" else f"p-{repository[-8:]}"
    p = models.Project(key=key, name="p", repository=repository, status=status)
    db.add(p)
    db.commit()
    return p


def _handoff_db(monkeypatch, grant=None):
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", SHA)
    db = _db()
    if grant:
        from app import control
        control.issue_standing(db, grant, SHA, founder=FOUNDER_ID)
    return db


def _code(title, reason="", **extra):
    return [{"title": title, "reason": reason, "kind": "code_change", **extra}]


def test_a_code_change_goes_to_the_developer_as_a_scoped_backlog_proposal_without_a_grant(monkeypatch):
    db = _handoff_db(monkeypatch)
    db.add(models.Agent(key="developer", role="developer", enabled=True)) if not db.scalar(
        select(models.Agent).where(models.Agent.key == "developer")) else None
    parent = _task(db)
    _run(db, parent, _code(f"Add a boundary test in {METRICS}", "a reading five minutes ahead is accepted"))
    k, = _children(db)
    dev = db.scalar(select(models.Agent).where(models.Agent.key == "developer"))
    # Break caught: the proposal stays unassigned prose (before), or runs with no founder grant (worse).
    assert k.status == enums.TaskStatus.BACKLOG and k.assigned_agent_id == dev.id
    assert k.context["policy"] == "HANDOFF" and k.context["handoff"] == {"from": parent.key, "run": k.context["origin_run"]}
    assert k.context["write"] is True and k.context["stage_c"] == {"paths": [METRICS]}
    assert k.context["autonomous"] is False and "independent provider" in k.success_criteria


def test_a_standing_grant_that_covers_every_named_file_makes_the_handoff_ready(monkeypatch):
    db = _handoff_db(monkeypatch, grant=[METRICS])
    parent = _task(db, project_id=_project(db).id)
    out = _run(db, parent, _code(f"Cover the skew limit in {METRICS}"))
    k, = _children(db)
    assert k.status == enums.TaskStatus.READY and k.context["autonomous"] is True
    mat = db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]
    assert mat["handoff"] == [{"task": k.key, "paths": [METRICS], "ready": True}] and mat["auto_ready"] == []


def test_a_standing_grant_cannot_make_a_read_only_project_handoff_ready(monkeypatch):
    from app import control

    db = _handoff_db(monkeypatch, grant=[METRICS])
    unit = models.BusinessUnit(key="youtube", name="YouTube", status=enums.BusinessUnitStatus.ACTIVE)
    project = models.Project(key="youtube-auto", name="YouTube automation pipeline",
                             repository="https://github.com/semihbugrasezer/youtube-auto",
                             business_unit=unit, status=enums.ProjectStatus.ACTIVE)
    db.add(project)
    db.commit()
    parent = _task(db, project_id=project.id)
    _run(db, parent, _code(f"Cover the skew limit in {METRICS}"))
    task, = _children(db)
    assert task.status == enums.TaskStatus.BACKLOG and task.context["autonomous"] is False
    assert control.standing_covers(db, [METRICS], SHA)
    assert task.context["stage_c"] == {"paths": [METRICS]}


@pytest.mark.parametrize("label,title,grant", [
    ("grant does not cover it", f"Edit {METRICS}", ["tests/test_goals.py"]),
    ("a grant for another SHA", f"Edit {METRICS}", "other-sha"),
    ("no file named", "Make the metrics module faster", [METRICS]),
    ("protected file", "Change app/main.py to loosen the auth check", [METRICS]),
    ("protected plus allowed", f"Edit {METRICS} and docker-compose.yml", [METRICS]),
])
def test_handoff_is_never_ready_outside_the_founders_grant(monkeypatch, label, title, grant):
    if grant == "other-sha":
        db = _handoff_db(monkeypatch)
        from app import control
        control.issue_standing(db, [METRICS], "b" * 40, founder=FOUNDER_ID)
    else:
        db = _handoff_db(monkeypatch, grant=grant)
    parent = _task(db, project_id=_project(db).id)
    _run(db, parent, _code(title))
    k, = _children(db)
    # "protected plus allowed": the intent is wider than the allow-list, so nothing is scoped at all.
    assert k.status == enums.TaskStatus.BACKLOG and k.context["autonomous"] is False, label
    if label in ("protected file", "protected plus allowed", "no file named"):
        assert "stage_c" not in k.context and "write" not in k.context, label


@pytest.mark.parametrize("project", [None, "other-repo", "paused"])
def test_handoff_without_an_admissible_project_never_becomes_autonomous(monkeypatch, project):
    from app import task_materializer

    db = _handoff_db(monkeypatch, grant=[METRICS])
    pid = {None: None,
           "other-repo": _project(db, "https://github.com/semihbugrasezer/seerxo").id,
           "paused": _project(db, status=enums.ProjectStatus.PAUSED).id}[project]
    parent = _task(db, project_id=pid)
    # The project may be paused after an admitted run finishes but before its proposals are routed.
    run = models.AgentRun(task_id=parent.id, agent_id=parent.assigned_agent_id, provider="claude",
                          status=enums.AgentRunStatus.SUCCEEDED,
                          supervisor_verdict=enums.SupervisorVerdict.APPROVED)
    db.add(run)
    db.flush()
    task_materializer.materialize(db, parent, run,
                                 [worker.TaskCandidate(**c) for c in _code(f"Cover the skew limit in {METRICS}")])
    db.commit()
    k, = _children(db)
    # Break caught: a covered grant alone marks a no-project / foreign-repo task autonomous.
    assert k.status == enums.TaskStatus.BACKLOG and k.context["autonomous"] is False


def test_side_effect_wording_still_needs_approval_and_a_research_candidate_is_unchanged(monkeypatch):
    db = _handoff_db(monkeypatch, grant=[METRICS])
    parent = _task(db, project_id=_project(db).id)
    _run(db, parent, [*_code(f"Deploy a change to {METRICS}"), {"title": "Investigate signups", "reason": "why", "kind": "research"}])
    kids = {k.title: k for k in _children(db)}
    assert kids[f"Deploy a change to {METRICS}"].context["policy"] == "APPROVAL"
    assert kids[f"Deploy a change to {METRICS}"].approval_required is True
    assert kids[f"Deploy a change to {METRICS}"].status == enums.TaskStatus.BACKLOG
    assert kids["Investigate signups"].context["policy"] in ("AUTO", "BACKLOG")


def _chain(db, monkeypatch, handoff_title, *, grant_ok=True):
    """CEO (depth 0) proposes research; the research run proposes `handoff_title`. Returns (ceo, research, runs)."""
    monkeypatch.setenv("PROPOSAL_AUTO_READY", "true")
    goal = models.Goal(title="MRR $100", metric="mrr", target="100", status=enums.GoalStatus.ACTIVE)
    db.add(goal)
    db.commit()
    ceo = _task(db, goal_id=goal.id, project_id=_project(db).id, business_unit_id=None)
    _run(db, ceo, [{"title": "Investigate metric skew", "reason": "readings drift", "kind": "research"}])
    research, = _children(db)
    assert research.status == enums.TaskStatus.READY and research.context["depth"] == 1
    research.context = {**research.context, "verify": {"must_contain": ["selftest-x", research.key]}}
    db.commit()
    nxt = _good(research.key, new_task_candidates=_code(handoff_title, "a reading ahead is accepted")
                + [{"title": "More research", "reason": "x", "kind": "research"}],
                evidence=[f"token selftest-x and {research.key}"])
    out = worker.run_once(db, {"claude": FakeProvider("claude", [nxt]), "codex": FakeProvider("codex", [nxt])}, SPECS)
    assert out["task"] == research.key and out["verdict"] == "APPROVED", out
    return ceo, research, out


def test_ceo_to_research_to_developer_is_the_longest_chain_and_follows_the_grant(monkeypatch):
    db = _handoff_db(monkeypatch, grant=[METRICS])
    ceo, research, out = _chain(db, monkeypatch, f"Add a boundary test in {METRICS}")
    dev, = [k for k in _children(db) if k.id != research.id]
    mat = db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]
    # Break caught: MAX_DEPTH=1 silently drops the developer task; or the research fan-out is accepted too.
    assert dev.context["depth"] == 2 and dev.context["handoff"]["from"] == research.key
    assert dev.goal_id == ceo.goal_id and dev.project_id == ceo.project_id
    assert dev.status == enums.TaskStatus.READY and dev.context["autonomous"] is True
    assert dev.context["stage_c"] == {"paths": [METRICS]} and "recorded output" in dev.success_criteria
    assert mat["dropped"] == 1 and len(mat["created"]) == 1
    # Terminal: neither the developer's task nor a depth-2 research run can propose again.
    from app import task_materializer
    run = models.AgentRun(task_id=dev.id, agent_id=dev.assigned_agent_id, provider="claude",
                          status=enums.AgentRunStatus.SUCCEEDED,
                          supervisor_verdict=enums.SupervisorVerdict.APPROVED)
    db.add(run)
    db.flush()
    cands = [worker.TaskCandidate(title="Add another test in tests/test_goals.py", reason="r", kind="code_change")]
    outcome = task_materializer.materialize(db, dev, run, cands)
    assert outcome["created"] == [] and "developer" in outcome["reason"]
    assert len(_children(db)) == 2


def test_the_chain_without_a_grant_leaves_the_developer_task_pending_in_backlog(monkeypatch):
    db = _handoff_db(monkeypatch)
    _, research, _ = _chain(db, monkeypatch, f"Add a boundary test in {METRICS}")
    dev, = [k for k in _children(db) if k.id != research.id]
    assert dev.context["depth"] == 2 and dev.status == enums.TaskStatus.BACKLOG and dev.context["autonomous"] is False


def test_the_terminal_hand_off_cannot_be_reached_by_a_non_research_or_handed_off_parent(monkeypatch):
    db = _handoff_db(monkeypatch, grant=[METRICS])
    from app import task_materializer
    pid = _project(db).id
    cands = [worker.TaskCandidate(title=f"Edit {METRICS}", reason="r", kind="code_change")]
    dev = db.scalar(select(models.Agent).where(models.Agent.key == "developer"))
    # Fixed keys: a hash-derived key depends on PYTHONHASHSEED and can collide with another task row.
    for n, (label, ctx, agent) in enumerate([("depth-1 non-AUTO", {"depth": 1, "policy": "BACKLOG", "autonomous": True}, None),
                                             ("depth-2", {"depth": 2, "policy": "AUTO", "autonomous": True}, None),
                                             ("developer owner", {"depth": 0}, dev)]):
        parent = _task(db, key=f"TASK-{9200 + n}", project_id=pid, context=ctx,
                       **({"assigned_agent_id": agent.id} if agent else {}))
        run = models.AgentRun(task_id=parent.id, agent_id=parent.assigned_agent_id,
                              status=enums.AgentRunStatus.SUCCEEDED,
                              supervisor_verdict=enums.SupervisorVerdict.APPROVED)
        db.add(run)
        db.flush()
        assert task_materializer.materialize(db, parent, run, cands)["created"] == [], label
    assert _children(db) == []


def test_a_handed_off_task_runs_nothing_while_writes_are_off(monkeypatch):
    db = _handoff_db(monkeypatch, grant=[METRICS])
    parent = _task(db, project_id=_project(db).id)
    _run(db, parent, _code(f"Cover the skew limit in {METRICS}"))
    k, = _children(db)
    out = worker.run_once(db, {"claude": FakeProvider("claude", [])}, SPECS)
    # Break caught: a READY write task executes (or is claimed) with the global flag off.
    assert out.get("model_calls", 0) == 0 and db.get(models.Task, k.id).status == enums.TaskStatus.READY
    assert {s["reason"] for s in out.get("skipped", [])} <= {"code_write_disabled", "agent_not_in_registry"}


# --- the hand-off also works from a research task that an agent itself proposed (depth 1 -> 2) ---------------
def _depth1_research(db):
    return _task(db, project_id=_project(db).id,
                 context={"autonomous": True, "scope": "company-os", "depth": 1, "policy": "AUTO",
                          "verify": {"must_contain": ["selftest-x", "TASK-0100"]}})


def test_ceo_to_research_to_developer_end_to_end(monkeypatch):
    monkeypatch.setenv("PROPOSAL_AUTO_READY", "true")
    db = _handoff_db(monkeypatch, grant=[METRICS])
    ceo = _task(db, key="TASK-0090", project_id=_project(db).id, context={"autonomous": True, "scope": "company-os", "rule": "GOAL_NO_WORK",
                                                "verify": {"must_contain": ["selftest-x", "TASK-0090"]}})
    _run(db, ceo, [{"title": "Investigate the future-skew edge", "reason": "is the boundary tested?", "kind": "research"}])
    research, = _children(db)
    assert research.context["depth"] == 1 and research.status == enums.TaskStatus.READY
    research.context = {**research.context, "verify": {"must_contain": ["selftest-x", research.key]}}
    db.commit()
    out = _run(db, research, _code(f"Add the boundary test to {METRICS}", "five minutes ahead is accepted"))
    dev = [k for k in _children(db) if k.context.get("kind") == "code_change"]
    # Break caught: the hand-off worked only for a directly created research task, never for the CEO's.
    assert len(dev) == 1 and dev[0].context["depth"] == 2 and dev[0].context["policy"] == "HANDOFF"
    assert dev[0].status == enums.TaskStatus.READY and dev[0].context["stage_c"] == {"paths": [METRICS]}
    assert dev[0].context["handoff"]["from"] == research.key
    assert db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]["handoff"][0]["ready"] is True


def test_at_depth_one_only_a_code_change_may_go_deeper_and_the_rest_is_counted_as_dropped(monkeypatch):
    db = _handoff_db(monkeypatch)
    out = _run(db, _depth1_research(db), [
        {"title": "Investigate more", "reason": "r", "kind": "research"},
        {"title": "Plan the quarter", "reason": "p", "kind": "planning"},
        *_code(f"Edit {METRICS}")])
    mat = db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]
    assert [k.context["kind"] for k in _children(db)] == ["code_change"] and mat["dropped"] == 2


def test_the_developer_task_is_terminal_and_a_depth_one_task_with_no_code_change_still_drops_everything(monkeypatch):
    db = _handoff_db(monkeypatch)
    deep = _task(db, key="TASK-0095", context={"autonomous": True, "scope": "company-os", "depth": 2,
                                                 "verify": {"must_contain": ["selftest-x", "TASK-0095"]}})
    out = _run(db, deep, _code(f"Edit {METRICS}"))
    assert out["verdict"] == "APPROVED" and _children(db) == []       # depth 2 proposes nothing, not even a code_change
    assert "depth limit" in db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]["reason"]
    shallow = _depth1_research(db)
    shallow.key = "TASK-0096"; shallow.context = {**shallow.context, "verify": {"must_contain": ["selftest-x", "TASK-0096"]}}
    db.commit()
    out = _run(db, shallow, _cands(2))                                  # research/other kinds: unchanged behaviour
    assert _children(db) == [] and "depth limit" in db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]["reason"]


def test_a_handoff_from_depth_one_keeps_dedupe_and_the_per_run_cap(monkeypatch):
    db = _handoff_db(monkeypatch)
    parent = _depth1_research(db)
    cands = [{"title": f"Edit {METRICS} case {i}", "reason": "r", "kind": "code_change"} for i in range(5)]
    out = _run(db, parent, cands)
    assert len(_children(db)) == 3 and db.get(models.AgentRun, out["run_id"]).tool_calls["materialized"]["dropped"] == 2
