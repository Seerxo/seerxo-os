"""Active goals become work by deterministic rules, once per rule, never on every tick."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from app import audit, business_reconciler as br, enums, metrics_store, models, schemas

NOW = datetime.now(timezone.utc)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "lifetime"}


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    unit = models.BusinessUnit(key="youtube", name="AxiomTales", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1)
    db.add_all([unit, models.Agent(key="ceo", role="ceo", enabled=True),
                models.Agent(key="developer", role="dev", enabled=True)])
    db.flush()
    db.add(models.Project(key="axiomtales", name="AxiomTales", business_unit_id=unit.id,
                          status=enums.ProjectStatus.ACTIVE))
    db.commit()
    return db


def _goal(db, days_in=30, days_total=100):
    project = db.execute(select(models.Project)).scalar_one()
    g = models.Goal(title="Reach 10k views", metric="Lifetime views", baseline="0", target="10000",
                    status=enums.GoalStatus.ACTIVE, priority=enums.Priority.P1, project_id=project.id,
                    metric_selector=SEL, created_at=NOW - timedelta(days=days_in),
                    deadline=NOW + timedelta(days=days_total - days_in))
    db.add(g)
    db.commit()
    return g


def _observe(db, value, at=NOW, source="youtube-data-api"):
    metrics_store.record(db, schemas.MetricsIn(business_unit="youtube", observations=[schemas.MetricIn(
        **SEL, value=value, source=source, observed_at=at)]), now=NOW)


def _rules(db):
    return sorted((t.context["rule"], db.get(models.Agent, t.assigned_agent_id).key)
                  for t in db.execute(select(models.Task)).scalars())


def test_unmeasured_goal_asks_for_the_measurement_and_nothing_else():
    db = _db()
    _goal(db)
    br.reconcile(db, NOW)
    # Break caught: the CEO is asked to plan against a number nobody has.
    assert _rules(db) == [("GOAL_UNMEASURED", "developer")]
    _observe(db, 5000, at=NOW - timedelta(days=9))  # STALE is still unmeasured
    br.reconcile(db, NOW)
    assert _rules(db) == [("GOAL_UNMEASURED", "developer")]


def test_ambiguous_measurement_opens_only_the_attended_measurement_task():
    db = _db()
    _goal(db)
    _observe(db, 9_000, source="youtube-data-api")
    _observe(db, 10_500, source="youtube-auto")
    br.reconcile(db, NOW)
    # Break caught: one source arbitrarily closes a goal while another disagrees.
    assert _rules(db) == [("GOAL_UNMEASURED", "developer")]


def test_future_or_malformed_measurement_opens_only_the_measurement_task():
    db = _db()
    future = _goal(db)
    _observe(db, 10_500, at=NOW + timedelta(minutes=1))
    malformed = _goal(db)
    malformed.metric_selector = {"subject": "youtube:channel"}
    db.commit()
    br.reconcile(db, NOW)
    tasks = db.execute(select(models.Task).order_by(models.Task.goal_id)).scalars().all()
    assert [t.goal_id for t in tasks] == [future.id, malformed.id]
    assert tasks[0].context["rule"] == "GOAL_UNMEASURED"
    assert db.get(models.Agent, tasks[0].assigned_agent_id).key == "developer"
    assert tasks[1].context["rule"] == "GOAL_CONFIG_INVALID"  # a malformed selector is the goal's fault


def test_on_track_goal_without_work_gets_one_planning_task_across_ticks():
    db = _db()
    g = _goal(db)
    _observe(db, 3500)  # 35% at 30% of the time
    first = br.reconcile(db, NOW)
    for _ in range(3):  # the watchdog runs every 10 minutes
        assert br.reconcile(db, NOW) == []
    task = db.execute(select(models.Task)).scalar_one()
    assert first == [task.key] and _rules(db) == [("GOAL_NO_WORK", "ceo")]
    assert task.goal_id == g.id and task.business_unit_id is not None
    assert task.status == enums.TaskStatus.READY and task.context["autonomous"] is False


def test_behind_trajectory_asks_for_a_replan_even_with_open_work():
    db = _db()
    g = _goal(db, days_in=50)
    db.add(models.Task(key="TASK-0100", title="existing", objective="x", success_criteria="x", goal_id=g.id, status=enums.TaskStatus.IN_PROGRESS))
    db.commit()
    _observe(db, 3000)  # 30% at 50% of the time
    br.reconcile(db, NOW)
    task = db.execute(select(models.Task).where(models.Task.created_by == "business-reconciler")).scalar_one()
    assert task.context["rule"] == "GOAL_BEHIND" and "30%" in task.objective and "50%" in task.objective


def test_a_closed_task_is_not_reopened_until_the_cooldown_passes():
    db = _db()
    _goal(db)
    _observe(db, 3500)
    br.reconcile(db, NOW)
    task = db.execute(select(models.Task)).scalar_one()
    task.status = enums.TaskStatus.CANCELLED
    db.commit()
    # Break caught: finishing the plan is answered ten minutes later by the same plan.
    assert br.reconcile(db, NOW) == []
    later = metrics_store._aware(task.updated_at) + br.COOLDOWN + timedelta(minutes=1)
    _observe(db, 3500, at=later)
    assert len(br.reconcile(db, later)) == 1


def test_inactive_goals_are_ignored_and_the_worker_spends_nothing():
    from app import registry, worker
    db = _db()
    _goal(db).status = enums.GoalStatus.PROPOSED
    db.commit()
    assert br.reconcile(db, NOW) == []
    _goal(db)
    br.reconcile(db, NOW)
    assert worker.run_once(db, {}, registry.load())["model_calls"] == 0


def test_a_goal_without_a_selector_opens_nothing():
    # Live 2026-10-06: three ACTIVE goals had no selector (backup, Seerxo baseline, publishing);
    # each would have opened a "restore the feed" task with no feed to restore.
    db = _db()
    g = _goal(db)
    g.metric_selector = None
    db.commit()
    assert br.reconcile(db, NOW) == []


def test_an_automatic_task_says_why_it_exists_on_the_desk():
    from app import desk
    db = _db()
    g = _goal(db)
    _observe(db, 3500)
    (key,) = br.reconcile(db, NOW)
    task = db.execute(select(models.Task).where(models.Task.key == key)).scalar_one()
    detail = desk.task_detail(db, task)
    assert detail["origin"] == {"by": "business-reconciler", "automatic": True,
                                "trigger": f"GOAL_NO_WORK on goal {g.id}",
                                "reason": f"GOAL_NO_WORK on goal {g.id}"}
    assert detail["goal"] == {"id": g.id, "title": g.title, "status": "ACTIVE"}
    founder = models.Task(title="x", success_criteria="y", created_by="founder",
                          context={"source": "founder-desk-command"})
    assert desk.origin(founder) == {"by": "founder", "automatic": False, "trigger": "Founder Desk request"}


def test_planning_runs_unattended_only_when_the_founder_switches_it_on(monkeypatch):
    from app import registry, worker
    specs = registry.load()
    db = _db()
    _goal(db)
    _observe(db, 3500)
    br.reconcile(db, NOW)
    task = db.execute(select(models.Task)).scalar_one()
    assert worker._admission_gate(db, task, specs)[0] == "not_opted_in"  # default: attended
    db.delete(task)  # start over with the switch on
    db.commit()
    monkeypatch.setenv("RECONCILER_AUTONOMOUS_PLANNING", "true")
    br.reconcile(db, NOW)
    task = db.execute(select(models.Task).where(models.Task.status == enums.TaskStatus.READY)).scalar_one()
    # Break caught: the switch is on yet the task fails the worker's own quality gate.
    assert task.context["rule"] == "GOAL_NO_WORK" and worker._admission_gate(db, task, specs) == (
        None, specs["ceo"])


def test_the_measurement_rule_stays_attended_with_the_switch_on(monkeypatch):
    monkeypatch.setenv("RECONCILER_AUTONOMOUS_PLANNING", "true")
    db = _db()
    _goal(db)
    br.reconcile(db, NOW)
    assert [t.context["autonomous"] for t in db.execute(select(models.Task)).scalars()] == [False]


@pytest.mark.parametrize("close,auto_ready,kind,creator,expected", [
    (False, False, "other", "business-reconciler", enums.TaskStatus.REVIEW),
    (True, True, "other", "business-reconciler", enums.TaskStatus.REVIEW),
    (True, False, "research", "business-reconciler", enums.TaskStatus.REVIEW),
    (True, True, "research", "founder", enums.TaskStatus.REVIEW),
    (True, True, "research", "business-reconciler", enums.TaskStatus.DONE),
    # Review of #396 (P3-b): a task created over HTTP that only claims created_by="business-reconciler" never auto-closes.
    (True, True, "research", "service:admin", enums.TaskStatus.REVIEW),
    # Review of #405: the creator must be proven by exactly one task.create row; a second row or none fails closed.
    (True, True, "research", "duplicate-create", enums.TaskStatus.REVIEW),
    (True, True, "research", "no-create-row", enums.TaskStatus.REVIEW),
])
def test_goal_gap_closes_only_after_an_owned_read_only_child(monkeypatch, close, auto_ready, kind, creator, expected):
    """The whole loop: verified goal -> CEO plan -> a policy-routed child -> review or done."""
    from app import registry, worker
    from tests.test_worker import FakeProvider
    monkeypatch.setenv("RECONCILER_AUTONOMOUS_PLANNING", "true")
    monkeypatch.setenv("AUTO_CLOSE_GOAL_NO_WORK", str(close).lower())
    monkeypatch.setenv("PROPOSAL_AUTO_READY", str(auto_ready).lower())
    db = _db()
    db.add(models.Agent(key="research", role="research", enabled=True))
    db.commit()
    g = _goal(db)
    _observe(db, 3500)
    (key,) = br.reconcile(db, NOW)
    task = db.execute(select(models.Task).where(models.Task.key == key)).scalar_one()
    if creator.startswith("service:"):  # the claim stays; the recorded creator is an HTTP principal
        db.execute(update(models.AuditLog).where(models.AuditLog.task_id == task.id,
                                                 models.AuditLog.action == "task.create").values(actor=creator))
    elif creator == "duplicate-create":   # a later, second creation row makes the creator ambiguous
        audit.record(db, actor="service:admin", action="task.create", entity_type="task", entity_key=task.key,
                     task_id=task.id)
    elif creator == "no-create-row":
        db.execute(delete(models.AuditLog).where(models.AuditLog.task_id == task.id,
                                                 models.AuditLog.action == "task.create"))
    else:
        task.created_by = creator
    db.commit()
    plan = {"task_id": key, "agent_id": "ceo", "status": "completed",
            "summary": "Next step: publish two more episodes in the series this week",
            "actions": [], "evidence": [f"goal {g.id} at 35% with no open work"], "artifacts": [],
            "learning_candidates": [], "approval_request": None, "error": None,
            "new_task_candidates": [{"title": "Study episode 12 audience", "reason": "measure series cadence",
                                     "kind": kind}]}
    out = worker.run_once(db, {"claude": FakeProvider("claude", [plan]),
                               "codex": FakeProvider("codex", [plan])}, registry.load())
    task = db.execute(select(models.Task).where(models.Task.key == key)).scalar_one()
    assert out["verdict"] == "APPROVED" and task.status == expected
    kid = db.execute(select(models.Task).where(models.Task.created_by == "task-materializer")).scalar_one()
    assert kid.goal_id == g.id and kid.status == (enums.TaskStatus.READY if auto_ready and kind == "research"
                                                  else enums.TaskStatus.BACKLOG), kid.context
    if expected == enums.TaskStatus.DONE:
        assert task.completed_at and task.artifacts["auto_close"]["task_keys"] == [kid.key]
        assert kid.assigned_agent_id is not None and kid.success_criteria
    else:
        assert task.completed_at is None and "auto_close" not in task.artifacts
    assert br.reconcile(db, NOW) == []  # the proposal is work for the goal: no second plan


def test_a_met_goal_asks_the_founder_to_close_it_and_is_not_replanned(monkeypatch):
    monkeypatch.setenv("RECONCILER_AUTONOMOUS_PLANNING", "true")
    db = _db()
    _goal(db, days_in=90)
    _observe(db, 10_500)
    br.reconcile(db, NOW)
    [task] = db.execute(select(models.Task)).scalars().all()
    # Break caught: a reached goal is planned again or reported as behind.
    assert task.context["rule"] == "GOAL_ACHIEVED" and task.approval_required
    assert task.context["autonomous"] is False and task.assigned_agent_id is None
    assert "10500.0" in task.objective and "youtube-data-api" in task.objective
    assert br.reconcile(db, NOW) == []


def test_a_decreasing_target_counts_as_met_when_the_value_falls_to_it():
    db = _db()
    g = _goal(db)
    g.baseline, g.target = "100", "20"   # e.g. churned customers per month
    db.commit()
    _observe(db, 18)
    br.reconcile(db, NOW)
    assert [t.context["rule"] for t in db.execute(select(models.Task)).scalars()] == ["GOAL_ACHIEVED"]


def test_the_close_request_reaches_the_founders_needs_you_list():
    from app import desk
    db = _db()
    _goal(db, days_in=90)
    _observe(db, 10_500)
    br.reconcile(db, NOW)
    task = db.execute(select(models.Task)).scalar_one()
    items = desk.needs_you([], [], [task], {}, None, False)
    assert [i["target"]["key"] for i in items if i["kind"] == "APPROVAL"] == [task.key]


def _task(db):
    return db.execute(select(models.Task)).scalar_one()


def test_achieved_task_carries_exact_evidence():
    import json
    db = _db()
    g = _goal(db, days_in=90)
    _observe(db, 10_500)
    br.reconcile(db, NOW)
    ev = _task(db).context["evidence"]
    row = db.scalar(select(models.MetricObservation))
    assert ev["rule"] == "GOAL_ACHIEVED" and ev["goal_id"] == g.id and ev["observation_id"] == row.id
    assert (ev["measurement_status"], ev["reason_code"], ev["source"], ev["value"]) == (
        "VERIFIED", "MEASUREMENT_VERIFIED", "youtube-data-api", 10_500)
    assert ev["observed_at"] == NOW.isoformat() and ev["missing_evidence"] == [] and ev["progress"] == 1.05
    assert ev["selector"] == SEL and ev["evidence_timestamp"] == NOW.isoformat()
    assert json.loads(json.dumps(ev)) == ev and len(json.dumps(ev)) < 2000


def test_unmeasured_task_explains_why_without_trusting_a_value():
    db = _db()
    _goal(db)
    _observe(db, 9_000, source="youtube-data-api")
    _observe(db, 10_500, source="youtube-auto")
    br.reconcile(db, NOW)
    task = _task(db)
    ev = task.context["evidence"]
    assert (ev["reason_code"], ev["missing_evidence"]) == ("SOURCE_AMBIGUOUS", ["SOURCE_AMBIGUOUS"])
    assert ev["source"] is None and ev["value"] is None and ev["observation_id"] is None
    assert "several sources" in task.objective and "VERIFIED" in ev["would_change_if"]


def test_behind_task_records_its_provenance():
    db = _db()
    g = _goal(db, days_in=50)
    db.add(models.Task(key="TASK-0100", title="existing", objective="x", success_criteria="x", goal_id=g.id,
                       status=enums.TaskStatus.IN_PROGRESS))
    db.commit()
    _observe(db, 3000)
    br.reconcile(db, NOW)
    ev = db.execute(select(models.Task).where(models.Task.created_by == "business-reconciler")).scalar_one().context["evidence"]
    assert ev["rule"] == "GOAL_BEHIND" and ev["progress"] == 0.3 and ev["expected_progress"] == 0.5
    assert ev["source"] == "youtube-data-api" and ev["observation_id"] is not None and "30%" in ev["why"]


def test_invalid_measurement_never_triggers_achievement_or_trajectory_planning():
    db = _db()
    g = _goal(db, days_in=90)
    _observe(db, 99_999, at=NOW + timedelta(minutes=1))   # future-dated: would read as achieved
    br.reconcile(db, NOW)
    assert [t.context["rule"] for t in db.execute(select(models.Task)).scalars()] == ["GOAL_UNMEASURED"]
    assert g.id and _task(db).context["evidence"]["reason_code"] == "FUTURE_OBSERVATION"


def test_explanation_change_does_not_duplicate_open_work_and_reruns_are_idempotent():
    db = _db()
    g = _goal(db)
    _observe(db, 3500)
    br.reconcile(db, NOW)
    task = _task(db)
    task.context = {k: v for k, v in task.context.items() if k != "evidence"}   # a historical task
    db.commit()
    assert br.reconcile(db, NOW) == [] and br.reconcile(db, NOW + timedelta(hours=1)) == []
    assert db.query(models.Task).count() == 1 and task.dedupe_key == f"goal:{g.id}:GOAL_NO_WORK"


def test_historical_contexts_without_evidence_remain_valid_task_contexts():
    from app import desk
    db = _db()
    g = _goal(db)
    db.add(models.Task(key="TASK-0200", title="old", success_criteria="x", created_by="business-reconciler", goal_id=g.id,
                       status=enums.TaskStatus.READY, context={"goal": g.id, "rule": "GOAL_NO_WORK"}))
    db.commit()
    assert desk.origin(db.scalar(select(models.Task))).get("automatic") is True


def test_evidence_does_not_change_approval_agent_or_autonomy():
    db = _db()
    _goal(db, days_in=90)
    _observe(db, 10_500)
    br.reconcile(db, NOW)
    task = _task(db)
    assert task.approval_required and task.assigned_agent_id is None and task.context["autonomous"] is False
    assert task.status == enums.TaskStatus.READY and task.dedupe_key.endswith(":GOAL_ACHIEVED")


def _kinds(db):
    return sorted(t.context["rule"] for t in db.execute(select(models.Task)).scalars())


@pytest.mark.parametrize("field,value,code", [("baseline", None, "BASELINE_INVALID"),
                                              ("baseline", "not a number", "BASELINE_INVALID"),
                                              ("baseline", "NaN", "BASELINE_INVALID"),
                                              ("target", "", "TARGET_INVALID"),
                                              ("target", "top 10", "TARGET_INVALID"),
                                              ("target", "Infinity", "TARGET_INVALID"),
                                              ("target", "0", "TARGET_EQUALS_BASELINE")])
@pytest.mark.parametrize("reading", ["verified", "missing", "stale"])
def test_invalid_goal_configuration_opens_only_a_config_repair_task(field, value, code, reading):
    db = _db()
    g = _goal(db, days_in=50)
    setattr(g, field, value)
    db.commit()
    if reading == "verified":
        _observe(db, 99_999)   # would read as achieved / would plan
    elif reading == "stale":
        _observe(db, 99_999, at=NOW - timedelta(days=9))
    br.reconcile(db, NOW)
    task = _task(db)   # exactly one task: no planning, achievement, trajectory or feed blame
    ev = task.context["evidence"]
    assert task.context["rule"] == "GOAL_CONFIG_INVALID" and task.goal_id == g.id
    assert (ev["reason_code"], ev["config_issues"]) == (code, [code])
    assert code in task.objective and "not changed automatically" in task.objective
    assert task.approval_required and task.assigned_agent_id is None and task.context["autonomous"] is False
    assert task.dedupe_key == f"goal:{g.id}:GOAL_CONFIG_INVALID"
    assert ev["measurement_status"] == {"verified": "VERIFIED", "missing": "UNAVAILABLE", "stale": "STALE"}[reading]
    # the reading is judged on its own: only a bad reading adds a measurement entry
    assert ev["missing_evidence"] == [code] + ([] if reading == "verified" else [ev["measurement_reason_code"]])
    json.loads(json.dumps(ev))
    goal_fields = (g.baseline, g.target)
    assert br.reconcile(db, NOW) == [] and (g.baseline, g.target) == goal_fields   # no auto-edit, no duplicate


@pytest.mark.parametrize("selector", [{"subject": "youtube:channel"}, "views", ["subject"], 7, {**SEL, "source": 3}])
def test_malformed_persisted_selector_is_a_config_repair_not_an_exception(selector, caplog):
    db = _db()
    g = _goal(db)
    g.metric_selector = selector
    db.commit()
    br.reconcile(db, NOW)
    task = _task(db)
    ev = task.context["evidence"]
    assert task.context["rule"] == "GOAL_CONFIG_INVALID" and ev["config_issues"] == ["SELECTOR_INVALID"]
    assert ev["selector"] in ({}, {"subject": "youtube:channel"}, {**{k: SEL[k] for k in SEL}})
    assert "failed for goal" not in caplog.text
    json.dumps(task.context)


def test_both_config_and_measurement_problems_are_reported_separately():
    db = _db()
    g = _goal(db)
    g.baseline = None
    db.commit()
    _observe(db, 9_000, source="youtube-data-api")
    _observe(db, 10_500, source="youtube-auto")
    br.reconcile(db, NOW)
    ev = _task(db).context["evidence"]
    assert ev["config_issues"] == ["BASELINE_INVALID"] and ev["measurement_reason_code"] == "SOURCE_AMBIGUOUS"
    assert ev["missing_evidence"] == ["BASELINE_INVALID", "SOURCE_AMBIGUOUS"]


def test_valid_goal_with_missing_or_stale_reading_is_still_unmeasured():
    db = _db()
    _goal(db)
    br.reconcile(db, NOW)
    _observe(db, 5000, at=NOW - timedelta(days=9))
    br.reconcile(db, NOW)
    assert _kinds(db) == ["GOAL_UNMEASURED"]


def test_config_repair_cooldown_and_autonomy_switch(monkeypatch):
    monkeypatch.setenv("RECONCILER_AUTONOMOUS_PLANNING", "true")
    db = _db()
    g = _goal(db)
    g.target = "x"
    db.commit()
    _observe(db, 3500)
    br.reconcile(db, NOW)
    task = _task(db)
    assert task.context["autonomous"] is False
    task.status = enums.TaskStatus.CANCELLED
    db.commit()
    assert br.reconcile(db, NOW) == []
    # Re-observe at the later time: the reading must still be fresh there, or the result depends on the window.
    later = metrics_store._aware(task.updated_at) + br.COOLDOWN + timedelta(minutes=1)
    _observe(db, 3500, at=later)
    assert len(br.reconcile(db, later)) == 1


@pytest.mark.parametrize("selector", [{}, [], "", 0, False])
@pytest.mark.parametrize("reading", [True, False])
def test_falsy_malformed_selector_is_a_config_repair_not_a_silent_skip(selector, reading, monkeypatch):
    monkeypatch.setenv("RECONCILER_AUTONOMOUS_PLANNING", "true")
    db = _db()
    g = _goal(db, days_in=90)
    g.metric_selector = selector
    db.commit()
    if reading:
        _observe(db, 99_999)   # a stray reading must not make the goal achieved or plannable
    br.reconcile(db, NOW)
    task = _task(db)   # exactly one
    ev = task.context["evidence"]
    assert task.context["rule"] == "GOAL_CONFIG_INVALID" and task.goal_id == g.id
    assert ev["config_issues"] == ["SELECTOR_INVALID"] and ev["reason_code"] == "SELECTOR_INVALID"
    assert task.approval_required and task.assigned_agent_id is None and task.context["autonomous"] is False
    json.dumps(task.context)
    assert br.reconcile(db, NOW) == [] and db.query(models.Task).count() == 1
