"""Operational goals: measured by one verified observation; never invented progress."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import desk, enums, goals, main, metrics_store, models, schemas

NOW = datetime.now(timezone.utc)
SEL = {"subject": "youtube:channel", "metric": "views", "window": "7d"}


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(models.BusinessUnit(key="youtube", name="AxiomTales", status=enums.BusinessUnitStatus.ACTIVE,
                               priority=enums.Priority.P1))
    db.commit()
    return db


def _observe(db, value, at=NOW, source="youtube-data-api"):
    metrics_store.record(db, schemas.MetricsIn(business_unit="youtube", observations=[schemas.MetricIn(
        **SEL, value=value, source=source, observed_at=at)]), now=NOW)


def _goal(db, **kw):
    body = dict(title="Grow channel reach", metric="Channel views, 7 days", baseline="10,000",
                target="20,000", selector=schemas.GoalSelector(**SEL), status=enums.GoalStatus.ACTIVE)
    return main.create_goal(schemas.GoalCreate(**(body | kw)), db)


def test_progress_is_unknown_until_a_verified_observation_exists():
    db = _db()
    g = _goal(db)
    # Break caught: a goal with no data rendered as 0% instead of "no verified observation".
    assert g["progress"] is None and g["latest"] is None and g["freshness"] == "UNAVAILABLE"
    _observe(db, 12_500)
    g = main.get_goal(g["id"], db)
    assert g["progress"] == 0.25 and g["latest"]["source"] == "youtube-data-api"
    assert g["freshness"] == "VERIFIED"


def test_newest_observation_wins_and_old_data_is_stale():
    db = _db()
    g = _goal(db)
    _observe(db, 11_000, at=NOW - timedelta(days=9))
    assert main.get_goal(g["id"], db)["freshness"] == "STALE"
    _observe(db, 15_000, at=NOW - timedelta(hours=1))  # newer reading of the same series
    g = main.get_goal(g["id"], db)
    assert g["latest"]["value"] == 15_000 and g["progress"] == 0.5 and g["freshness"] == "VERIFIED"


def test_unpinned_selector_with_multiple_sources_is_unavailable():
    db = _db()
    g = _goal(db)
    _observe(db, 12_500, source="youtube-data-api")
    _observe(db, 20_000, source="youtube-auto")
    view = main.get_goal(g["id"], db)
    assert view["freshness"] == "UNAVAILABLE"
    assert view["latest"] is None and view["progress"] is None


def test_source_pinned_selector_remains_authoritative_when_another_source_reports():
    db = _db()
    g = _goal(db, selector=schemas.GoalSelector(**SEL, source="youtube-data-api"))
    _observe(db, 12_500, source="youtube-data-api")
    _observe(db, 20_000, source="youtube-auto")
    view = main.get_goal(g["id"], db)
    assert view["freshness"] == "VERIFIED"
    assert view["latest"]["source"] == "youtube-data-api" and view["progress"] == 0.25


def test_future_dated_observation_is_not_goal_evidence():
    db = _db()
    g = _goal(db)
    # Relative to the real clock: progress() evaluates at call time, and NOW is fixed at import.
    _observe(db, 20_000, at=datetime.now(timezone.utc) + timedelta(minutes=1))  # within ingestion's clock-skew allowance
    view = main.get_goal(g["id"], db)
    assert view["freshness"] == "UNAVAILABLE"
    assert view["latest"] is None and view["progress"] is None


def test_legacy_selector_without_window_still_uses_the_default_window():
    db = _db()
    goal = models.Goal(title="legacy selector", metric="Views", baseline="0", target="10",
                       status=enums.GoalStatus.ACTIVE,
                       metric_selector={"subject": "youtube:channel", "metric": "views"})
    db.add(goal)
    metrics_store.record(db, schemas.MetricsIn(business_unit="youtube", observations=[schemas.MetricIn(
        subject="youtube:channel", metric="views", value=5, source="youtube-data-api", observed_at=NOW)]), now=NOW)
    db.commit()
    assert goals.progress(db, goal, NOW)["progress"] == 0.5


def test_malformed_persisted_selector_is_unavailable_not_an_exception():
    db = _db()
    goal = models.Goal(title="bad selector", metric="Views", target="10", status=enums.GoalStatus.ACTIVE,
                       metric_selector={"subject": "youtube:channel"})
    db.add(goal)
    db.commit()
    assert goals.progress(db, goal, NOW) == {
        "selector": {"subject": "youtube:channel"}, "baseline_value": None, "target_value": 10.0,
        "latest": None, "freshness": "UNAVAILABLE", "progress": None}


def test_non_finite_baseline_or_target_is_not_a_measurement():
    db = _db()
    g = _goal(db, baseline="NaN", target="Infinity")
    _observe(db, 12_000)
    view = main.get_goal(g["id"], db)
    assert view["baseline_value"] is None and view["target_value"] is None
    assert view["latest"]["value"] == 12_000 and view["progress"] is None


def test_mixed_units_for_one_series_are_unavailable():
    db = _db()
    g = _goal(db)
    _observe(db, 12_500)
    metrics_store.record(db, schemas.MetricsIn(business_unit="youtube", observations=[schemas.MetricIn(
        **SEL, value=12_500, unit="views", source="youtube-data-api",
        observed_at=NOW - timedelta(minutes=1))]), now=NOW)
    view = main.get_goal(g["id"], db)
    assert view["freshness"] == "UNAVAILABLE"
    assert view["latest"] is None and view["progress"] is None


def test_non_numeric_target_shows_value_but_no_percentage():
    db = _db()
    g = _goal(db, baseline=None, target="top 10 in category")
    _observe(db, 12_000)
    g = main.get_goal(g["id"], db)
    assert g["latest"]["value"] == 12_000 and g["progress"] is None


def test_active_goal_requires_a_metric_selector():
    db = _db()
    with pytest.raises(HTTPException) as e:
        _goal(db, selector=None)
    assert e.value.status_code == 422
    proposed = _goal(db, selector=None, status=enums.GoalStatus.PROPOSED)
    with pytest.raises(HTTPException) as e:
        main.update_goal(proposed["id"], schemas.GoalUpdate(status=enums.GoalStatus.ACTIVE), db)
    assert e.value.status_code == 422
    assert db.get(models.Goal, proposed["id"]).status == enums.GoalStatus.PROPOSED  # rolled back
    ok = main.update_goal(proposed["id"], schemas.GoalUpdate(status=enums.GoalStatus.ACTIVE,
                                                            selector=schemas.GoalSelector(**SEL)), db)
    assert ok["status"] == "ACTIVE"
    actions = db.scalars(select(models.AuditLog.action).where(models.AuditLog.entity_type == "goal")).all()
    assert actions.count("goal.update") == 1 and actions.count("goal.create") == 1


def test_tasks_link_to_open_goals_only():
    db = _db()
    g = _goal(db)
    task = main.create_task(schemas.TaskCreate(title="Ship thumbnails v2", success_criteria="CTR measured",
                                               goal=g["id"]), db)
    assert db.scalar(select(models.Task).where(models.Task.key == task.key)).goal_id == g["id"]
    assert [t["key"] for t in main.get_goal(g["id"], db)["tasks"]] == [task.key]
    with pytest.raises(HTTPException) as e:
        main.create_task(schemas.TaskCreate(title="x", success_criteria="yyy", goal=999), db)
    assert e.value.status_code == 404
    main.update_goal(g["id"], schemas.GoalUpdate(status=enums.GoalStatus.ABANDONED), db)
    with pytest.raises(HTTPException) as e:
        main.create_task(schemas.TaskCreate(title="x", success_criteria="yyy", goal=g["id"]), db)
    assert e.value.status_code == 409


def test_desk_priority_carries_measured_progress_and_serving_work():
    db = _db()
    g = _goal(db)
    _observe(db, 12_500)
    main.create_task(schemas.TaskCreate(title="Ship thumbnails v2", success_criteria="CTR measured",
                                        status=enums.TaskStatus.READY, goal=g["id"]), db)
    goal = db.get(models.Goal, g["id"])
    open_tasks = db.scalars(select(models.Task)).all()
    c = desk.current_priority([goal], open_tasks, {}, progress_of=lambda x: goals.progress(db, x))
    assert c["kind"] == "goal" and c["open_tasks"] == 1 and c["top_task"] == open_tasks[0].key
    assert c["measure"]["progress"] == 0.25 and c["measure"]["freshness"] == "VERIFIED"


@pytest.mark.parametrize("raw, expected", [
    (None, None), ("12,400", 12400.0), ("1_000", 1000.0), ("  7 ", 7.0), ("1e3", 1000.0)])
def test_number_parses_plain_and_formatted_values(raw, expected):
    assert goals.number(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "nan", "inf"])
def test_number_rejects_non_numeric_and_non_finite_text(raw):
    assert goals.number(raw) is None


@pytest.mark.parametrize("selector, expected", [
    ({"subject": "youtube:channel", "metric": "views"}, True),
    ({"subject": "youtube:channel", "metric": "views", "window": "7d"}, True),
    ({"subject": "youtube:channel", "metric": "views", "source": None}, True),
    ({"subject": "youtube:channel", "metric": "views", "window": "7d", "source": "youtube-data-api"}, True),
    (None, False),
    ("views", False),
    (["subject", "metric"], False),
    ({"metric": "views"}, False),
    ({"subject": "", "metric": "views"}, False),
    ({"subject": 5, "metric": "views"}, False),
    ({"subject": "youtube:channel"}, False),
    ({"subject": "youtube:channel", "metric": ""}, False),
    ({"subject": "youtube:channel", "metric": 5}, False),
    ({"subject": "youtube:channel", "metric": "views", "window": 7}, False),
    ({"subject": "youtube:channel", "metric": "views", "source": ""}, False),
    ({"subject": "youtube:channel", "metric": "views", "source": 5}, False),
])
def test_selector_valid_accepts_only_well_formed_selectors(selector, expected):
    assert goals.selector_valid(selector) is expected


def test_config_issues_are_independent_of_any_reading():
    ok = models.Goal(metric_selector=SEL, baseline="0", target="10")
    assert goals.config_issues(ok) == []
    assert goals.config_issues(models.Goal(metric_selector="views", baseline=None, target="x")) == [
        "SELECTOR_INVALID", "BASELINE_INVALID", "TARGET_INVALID"]
    assert goals.config_issues(models.Goal(metric_selector=SEL, baseline="5", target="5")) == ["TARGET_EQUALS_BASELINE"]
    assert goals.config_issues(models.Goal(metric_selector=SEL, baseline="NaN", target="Infinity")) == [
        "BASELINE_INVALID", "TARGET_INVALID"]
