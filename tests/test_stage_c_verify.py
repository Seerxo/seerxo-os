"""After the merge: verified only when main CI and the live deploy both prove it; measured only
from a real reading after the window. Unknown is never success."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import enums, models, stage_c, stage_c_verify

MERGE = "c" * 40
T0 = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
SEL = {"subject": "youtube:abc", "metric": "views"}


def _merged(measure=None):
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    unit = models.BusinessUnit(key="youtube", name="YouTube")
    db.add(unit)
    t = models.Task(key="TASK-0090", title="fix", success_criteria="views up", priority=enums.Priority.P2)
    db.add(t)
    db.flush()
    for s in ("DETECTED", "PLANNED", "IMPLEMENTING", "TESTING", "INDEPENDENT_REVIEW", "PR_OPEN", "CI",
              "READY_TO_MERGE"):
        stage_c.advance(db, t, s, "test")
    stage_c.advance(db, t, "MERGED", "founder (github:f, via stage-c-ci)", {"merged_by": "f", "merge_commit": MERGE})
    if measure:
        t.context = {**t.context, "improvement": {**t.context["improvement"], "measure": measure}}
    return db, t, unit


def _observe(db, unit, value, at):
    db.add(models.MetricObservation(business_unit_id=unit.id, subject=SEL["subject"], metric="views",
                                    window="", value=value, source="youtube-data-api", observed_at=at))
    db.flush()


def stage(t):
    return t.context["improvement"]["stage"]


@pytest.mark.parametrize("facts", [
    {"main_ci": None, "live_contains_merge": True},
    {"main_ci": "success", "live_contains_merge": None},
    {"main_ci": "success", "live_contains_merge": False},
    {},
])
def test_not_verified_until_main_ci_and_the_live_deploy_both_prove_it(facts):
    db, t, _ = _merged()
    # Break caught: a merge counts as verified while main is untested or the code is not live.
    assert stage_c_verify.step(db, t, facts, now=T0)["outcome"] == "WAITING"
    assert stage(t) == "MERGED"


def test_a_red_main_after_merge_blocks_for_the_founder():
    db, t, _ = _merged()
    out = stage_c_verify.step(db, t, {"main_ci": "failure", "live_contains_merge": True}, now=T0)
    assert out["outcome"] == "MAIN_CI_FAILED" and t.status == enums.TaskStatus.BLOCKED and stage(t) == "MERGED"
    assert stage_c_verify.step(db, t, {"main_ci": "success", "live_contains_merge": True})["outcome"] == "SKIPPED"


def test_no_named_metric_is_measured_as_not_measurable():
    db, t, _ = _merged()
    ok = {"main_ci": "success", "live_contains_merge": True}
    assert stage_c_verify.step(db, t, ok, now=T0)["outcome"] == "VERIFIED"
    assert stage_c_verify.step(db, t, ok, now=T0)["outcome"] == "NOT_MEASURABLE" and stage(t) == "MEASURED"


def _for_goal(db, t, baseline="100", target="200", window=""):
    goal = models.Goal(title="views", metric="views", baseline=baseline, target=target,
                       metric_selector={**SEL, "window": window})
    db.add(goal)
    db.flush()
    t.goal_id = goal.id
    return goal


@pytest.mark.parametrize("baseline, target, later, outcome", [("100", "200", 150, "IMPROVED"),
                                                             ("100", "50", 150, "NOT_IMPROVED")])
def test_a_goal_task_without_a_named_metric_is_measured_on_the_goal_metric(baseline, target, later, outcome):
    """G9: no code sets improvement.measure, so every goal-driven change ended NOT_MEASURABLE. The goal's own
    selector and direction are used, recorded as measure_source "goal" (goal-wide, not this task's proven effect)."""
    db, t, unit = _merged()
    goal = _for_goal(db, t, baseline, target)
    _observe(db, unit, 100, T0 - timedelta(hours=1))
    ok = {"main_ci": "success", "live_contains_merge": True}
    assert stage_c_verify.step(db, t, ok, now=T0)["outcome"] == "VERIFIED"
    ev = t.context["improvement"]["history"][-1]["evidence"]
    assert ev["baseline"]["value"] == 100
    assert ev["measure"] == {"selector": {**SEL, "window": ""}, "direction": "up" if target == "200" else "down",
                             "window_hours": 24, "measure_source": "goal", "goal_id": goal.id}
    goal.target = "1"   # the recorded measure is judged, not one re-derived from a goal edited meanwhile
    _observe(db, unit, later, T0 + timedelta(hours=26))
    assert stage_c_verify.step(db, t, ok, now=T0 + timedelta(hours=27))["outcome"] == outcome


@pytest.mark.parametrize("window, hours", [("7d", 168), ("6h", 24), ("", 24)])
def test_a_goal_measure_waits_at_least_the_series_own_window(window, hours):
    db, t, _ = _merged()
    _for_goal(db, t, window=window)
    assert stage_c_verify._goal_measure(db, t)["window_hours"] == hours


@pytest.mark.parametrize("goal", [dict(baseline="x"), dict(target="100"), dict(window="weekly")])
def test_an_unevaluable_goal_or_unknown_window_stays_not_measurable(goal):
    db, t, _ = _merged()
    _for_goal(db, t, **goal)
    ok = {"main_ci": "success", "live_contains_merge": True}
    assert stage_c_verify.step(db, t, ok, now=T0)["outcome"] == "VERIFIED"
    assert stage_c_verify.step(db, t, ok, now=T0)["outcome"] == "NOT_MEASURABLE"


@pytest.mark.parametrize("direction, later, outcome", [("up", 150, "IMPROVED"), ("up", 90, "NOT_IMPROVED"),
                                                       ("down", 90, "IMPROVED")])
def test_a_named_metric_is_judged_on_the_first_reading_after_the_window(direction, later, outcome):
    db, t, unit = _merged({"selector": SEL, "direction": direction, "window_hours": 24})
    _observe(db, unit, 100, T0 - timedelta(hours=1))
    ok = {"main_ci": "success", "live_contains_merge": True}
    assert stage_c_verify.step(db, t, ok, now=T0)["outcome"] == "VERIFIED"
    assert t.context["improvement"]["history"][-1]["evidence"]["baseline"]["value"] == 100
    # Break caught: judging before the window, or on a reading taken before it ended.
    assert stage_c_verify.step(db, t, ok, now=T0 + timedelta(hours=2))["outcome"] == "WAITING"
    _observe(db, unit, later, T0 + timedelta(hours=3))
    assert stage_c_verify.step(db, t, ok, now=T0 + timedelta(hours=25))["outcome"] == "WAITING"
    _observe(db, unit, later, T0 + timedelta(hours=26))
    assert stage_c_verify.step(db, t, ok, now=T0 + timedelta(hours=27))["outcome"] == outcome
    assert stage(t) == "MEASURED"


@pytest.mark.parametrize("direction, first, latest", [("up", 150, 90), ("down", 50, 110)])
def test_a_delayed_worker_uses_the_first_reading_after_the_window(direction, first, latest):
    db, t, unit = _merged({"selector": SEL, "direction": direction, "window_hours": 24})
    _observe(db, unit, 100, T0 - timedelta(hours=1))
    stage_c_verify.step(db, t, {"main_ci": "success", "live_contains_merge": True}, now=T0)
    _observe(db, unit, latest, T0 + timedelta(hours=24))  # the boundary is not after the window
    _observe(db, unit, first, T0 + timedelta(hours=25))
    _observe(db, unit, latest, T0 + timedelta(hours=26))

    assert stage_c_verify.step(db, t, {}, now=T0 + timedelta(hours=27))["outcome"] == "IMPROVED"
    evidence = t.context["improvement"]["history"][-1]["evidence"]
    assert evidence["reading"] == {"value": first, "observed_at": (T0 + timedelta(hours=25)).isoformat()}


def test_the_baseline_excludes_readings_after_verification_time():
    db, t, unit = _merged({"selector": SEL, "direction": "up", "window_hours": 24})
    _observe(db, unit, 100, T0 - timedelta(hours=1))
    _observe(db, unit, 999, T0 + timedelta(hours=30))
    stage_c_verify.step(db, t, {"main_ci": "success", "live_contains_merge": True}, now=T0)
    assert t.context["improvement"]["history"][-1]["evidence"]["baseline"]["value"] == 100


def test_a_future_reading_cannot_finish_measurement_after_the_window():
    db, t, unit = _merged({"selector": SEL, "direction": "up", "window_hours": 24})
    _observe(db, unit, 100, T0 - timedelta(hours=1))
    stage_c_verify.step(db, t, {"main_ci": "success", "live_contains_merge": True}, now=T0)
    _observe(db, unit, 999, T0 + timedelta(hours=30))
    assert stage_c_verify.step(db, t, {}, now=T0 + timedelta(hours=25))["outcome"] == "WAITING"
    assert stage(t) == "VERIFYING"


@pytest.mark.parametrize("source, other_source, other_unit, outcome", [
    (None, "another-reporter", None, "WAITING"),
    ("youtube-data-api", "another-reporter", None, "IMPROVED"),
    ("youtube-data-api", "youtube-data-api", "percent", "WAITING"),
])
def test_first_reading_selection_preserves_source_and_unit_checks(source, other_source, other_unit, outcome):
    selector = {**SEL, "source": source}
    db, t, unit = _merged({"selector": selector, "direction": "up", "window_hours": 24})
    _observe(db, unit, 100, T0 - timedelta(hours=1))
    stage_c_verify.step(db, t, {"main_ci": "success", "live_contains_merge": True}, now=T0)
    _observe(db, unit, 150, T0 + timedelta(hours=25))
    db.add(models.MetricObservation(business_unit_id=unit.id, subject=SEL["subject"], metric="views",
                                    window="", value=1, source=other_source, unit=other_unit,
                                    observed_at=T0 + timedelta(hours=26)))
    db.flush()
    assert stage_c_verify.step(db, t, {}, now=T0 + timedelta(hours=27))["outcome"] == outcome


PUBLISHER = {"login": "circleci-app[bot]", "id": 127350680}
CONTEXTS = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")


def _status(context, state, id, creator=PUBLISHER):
    return {"id": id, "context": context, "state": state, "creator": {"id": creator["id"], "login": creator["login"]}}


def _fake(actions=(), statuses=(), ancestry="ahead", compare_raw=None):
    calls = []

    def run(argv, **kw):
        path = argv[4]
        calls.append(path)
        if "/compare/" in path:
            # What gh really prints: --jq renders an object as JSON but a bare string unquoted (ahead).
            jq = argv[argv.index("--jq") + 1]
            out = json.dumps({"status": ancestry}) if jq == "{status}" else f"{ancestry}\n"
            out = out if compare_raw is None else compare_raw
            return SimpleNamespace(returncode=0, stdout=out)
        if "/statuses" in path:
            assert f"/commits/{MERGE}/statuses" in path
            return SimpleNamespace(returncode=0, stdout=json.dumps(list(statuses)))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"total_count": len(actions), "workflow_runs": list(actions)}))
    return run, calls


def _gh(run, live="d" * 40, workflows=frozenset({".github/workflows/ci.yml"})):
    return stage_c_verify.gh_facts("o/r", MERGE, live, set(workflows), publisher=PUBLISHER,
                                   status_contexts=CONTEXTS, run=run)


GREEN_ACTIONS = [{"path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success",
                  "run_number": 5, "run_attempt": 2, "head_sha": MERGE, "head_branch": "main", "event": "push"}]
GREEN_STATUSES = [_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "success", 2)]


def test_main_is_green_only_when_actions_and_every_trusted_circleci_status_succeed():
    run, calls = _fake(GREEN_ACTIONS, GREEN_STATUSES)
    assert _gh(run) == {"main_ci": "success", "live_contains_merge": True}
    assert "event=push" in calls[0] and calls[-1].endswith(f"{MERGE}...{'d' * 40}")
    # Unknown live SHA: never assumed live.
    assert _gh(run, live=None)["live_contains_merge"] is None


@pytest.mark.parametrize("statuses, expected", [
    ([_status(CONTEXTS[0], "success", 1)], None),  # one required context missing: wait
    ([_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "pending", 2)], None),
    ([_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "failure", 2)], "failure"),
    ([_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "error", 2)], "failure"),
    ([_status(CONTEXTS[0], "failure", 1), _status(CONTEXTS[0], "success", 2), _status(CONTEXTS[1], "success", 3)], "success"),
    ([_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "success", 2),
      _status(CONTEXTS[1], "failure", 3, creator={"id": 1, "login": "spoof"})], None),  # a spoof means wait
])
def test_main_ci_follows_the_latest_trusted_status_per_context(statuses, expected):
    run, _ = _fake(GREEN_ACTIONS, statuses)
    assert _gh(run)["main_ci"] == expected


def test_a_spoofed_failure_is_a_problem_so_main_is_not_success_but_waits():
    run, _ = _fake(GREEN_ACTIONS, [_status(CONTEXTS[0], "success", 1),
                                   _status(CONTEXTS[1], "success", 2),
                                   _status(CONTEXTS[1], "success", 9, creator={"id": 1, "login": "spoof"})])
    assert _gh(run)["main_ci"] is None  # the spoof is never judged as a result, and never as success


def test_circleci_only_main_is_verified_once_actions_has_no_push_runs():
    run, calls = _fake([], GREEN_STATUSES)
    assert _gh(run, workflows=frozenset())["main_ci"] == "success"
    assert not any("/actions/" in path for path in calls)


@pytest.mark.parametrize("unused_statuses", [None, {"not": "a list"}, [None]])
def test_actions_only_main_verification_does_not_read_unused_commit_statuses(unused_statuses):
    calls = []

    def run(argv, **kwargs):
        path = argv[4]
        calls.append(path)
        if "/statuses" in path:
            return SimpleNamespace(returncode=1 if unused_statuses is None else 0,
                                   stdout=json.dumps(unused_statuses))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"total_count": 1, "workflow_runs": GREEN_ACTIONS}))

    result = stage_c_verify.gh_facts("o/r", MERGE, None, {".github/workflows/ci.yml"},
                                    publisher=PUBLISHER, status_contexts=(), run=run)
    assert result == {"main_ci": "success", "live_contains_merge": None}
    assert len(calls) == 1 and "event=push" in calls[0]


def test_main_verification_without_any_required_ci_source_is_refused():
    with pytest.raises(ValueError, match="required"):
        stage_c_verify.gh_facts("o/r", MERGE, None, set(), publisher=PUBLISHER, status_contexts=(),
                                run=lambda *args, **kwargs: pytest.fail("invalid policy must not query GitHub"))


@pytest.mark.parametrize("total", [-1, 0, 2, True, 100])
def test_main_workflow_pages_must_be_complete_and_bounded(total):
    rows = [{**GREEN_ACTIONS[0], "head_sha": MERGE, "head_branch": "main", "event": "push"}]
    if total == 100:
        rows += [{**rows[0], "path": ".github/workflows/other.yml"} for _ in range(99)]
    def run(argv, **kwargs):
        body = rows if argv[-1].startswith("[") else {"total_count": total, "workflow_runs": rows}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body))
    assert stage_c_verify.gh_facts("o/r", MERGE, None, {".github/workflows/ci.yml"},
                                   publisher=PUBLISHER, status_contexts=(), run=run)["main_ci"] is None


@pytest.mark.parametrize("wrong", [{"head_sha": "b" * 40}, {"head_branch": "other"}, {"event": "pull_request"}])
def test_main_workflow_evidence_must_match_the_merge_sha_main_and_push(wrong):
    rows = [{**GREEN_ACTIONS[0], "head_sha": MERGE, "head_branch": "main", "event": "push", **wrong}]
    def run(argv, **kwargs):
        body = rows if argv[-1].startswith("[") else {"total_count": 1, "workflow_runs": rows}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body))
    assert stage_c_verify.gh_facts("o/r", MERGE, None, {".github/workflows/ci.yml"},
                                   publisher=PUBLISHER, status_contexts=(), run=run)["main_ci"] is None


def test_no_evidence_at_all_is_never_success():
    run, _ = _fake([], [])
    assert _gh(run, workflows=frozenset())["main_ci"] is None
    with pytest.raises(ValueError):
        stage_c_verify.gh_facts("o/r", "main", None, set(), publisher=PUBLISHER, status_contexts=CONTEXTS, run=run)


def test_a_future_dated_reading_is_not_judged_before_the_window_ends():
    db, t, unit = _merged({"selector": SEL, "direction": "up", "window_hours": 24})
    _observe(db, unit, 100, T0 - timedelta(hours=1))
    ok = {"main_ci": "success", "live_contains_merge": True}
    stage_c_verify.step(db, t, ok, now=T0)
    _observe(db, unit, 999, T0 + timedelta(hours=30))  # a source with a wrong clock
    # Break caught: a reading stamped in the future measures the change before its window ends.
    assert stage_c_verify.step(db, t, ok, now=T0 + timedelta(hours=2))["outcome"] == "WAITING"


# --------------------------------------------------------------------------- live ancestry (B-12)

@pytest.mark.parametrize("ancestry, live", [
    ("ahead", True), ("identical", True),             # the live release contains the merge
    ("behind", False), ("diverged", False), ("something-new", False),
])
def test_only_ahead_or_identical_proves_the_merge_is_live(ancestry, live):
    run, _ = _fake(GREEN_ACTIONS, GREEN_STATUSES, ancestry=ancestry)
    assert _gh(run)["live_contains_merge"] is live


@pytest.mark.parametrize("raw", ["ahead\n", '"ahead"', "[]", "{}", '{"state": "ahead"}', "not json"])
def test_a_malformed_compare_answer_is_never_proof(raw):
    # Raises: the worker records WAITING evidence_unavailable and retries; it never reads it as live.
    run, _ = _fake(GREEN_ACTIONS, GREEN_STATUSES, compare_raw=raw)
    with pytest.raises((ValueError, KeyError, TypeError)):
        _gh(run)


def test_real_gh_output_takes_a_merged_task_past_merged_only_when_live():
    """End to end through gh_facts as the CLI really prints it (B-12: production never left MERGED)."""
    for ancestry, moved in (("behind", False), ("ahead", True)):
        db, t, _ = _merged()
        run, _ = _fake(GREEN_ACTIONS, GREEN_STATUSES, ancestry=ancestry)
        out = stage_c_verify.step(db, t, _gh(run), now=T0)
        assert (out["outcome"] != "WAITING") is moved
        assert (stage(t) != "MERGED") is moved
