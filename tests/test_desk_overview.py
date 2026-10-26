"""Overview truthfulness: no 'working' without recent evidence; priority only from real state."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import desk, enums, models, worker
from tests.test_founder_desk import _company


def _state(db):
    from app import registry
    return desk.build_state(db, registry.load(), False)


def _session():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return Session(engine)


def test_run_untouched_past_the_stale_limit_reads_stalled_not_running():
    db = _session()
    _company(db)
    dev = next(a for a in _state(db)["agents"] if a["key"] == "developer")
    assert dev["status"] == "RUNNING"  # fresh STARTED run
    old = datetime.now(timezone.utc) - worker.STALE_AFTER - timedelta(minutes=5)
    for run in db.query(models.AgentRun):
        run.started_at = old
    for task in db.query(models.Task):
        task.updated_at = old
    db.commit()
    # Break caught: the Desk said "4 working" while the worker had been dead for two days.
    dev = next(a for a in _state(db)["agents"] if a["key"] == "developer")
    assert dev["status"] == "STALLED"


def test_current_priority_prefers_an_active_goal_then_top_open_work():
    db = _session()
    task = _company(db)
    cp = _state(db)["current_priority"]
    assert cp["kind"] == "task" and cp["key"] == task.key and cp["owner"] == "developer"
    db.add(models.Goal(title="Seerxo revenue baseline", metric="mrr", target="verified",
                       status=enums.GoalStatus.ACTIVE, priority=enums.Priority.P1))
    db.add(models.Goal(title="proposed only", metric="x", target="y",
                       status=enums.GoalStatus.PROPOSED, priority=enums.Priority.P0))
    db.commit()
    cp = _state(db)["current_priority"]
    assert cp["kind"] == "goal" and cp["title"] == "Seerxo revenue baseline"


def test_no_goal_and_no_high_priority_work_means_no_priority():
    assert desk.current_priority([], [], {}) is None


def test_task_lead_time_uses_recent_done_tasks_and_keeps_empty_unknown():
    db = _session()
    _company(db)
    assert _state(db)["task_lead_time"] == {"median_hours": None, "sample_size": 0, "review_pending": 0}

    now = datetime.now(timezone.utc)
    for key, status, created_days, completed_days in (
        ("TASK-0050", enums.TaskStatus.DONE, 2, 1),
        ("TASK-0051", enums.TaskStatus.DONE, 7, 3),
        ("TASK-0052", enums.TaskStatus.DONE, 50, 31),
        ("TASK-0053", enums.TaskStatus.CANCELLED, 3, 1),
        ("TASK-0054", enums.TaskStatus.REVIEW, 5, None),
    ):
        db.add(models.Task(key=key, title=key, success_criteria="verified",
                           status=status, created_at=now - timedelta(days=created_days),
                           completed_at=now - timedelta(days=completed_days) if completed_days else None))
    db.commit()
    assert _state(db)["task_lead_time"] == {"median_hours": 60.0, "sample_size": 2, "review_pending": 1}


def test_needs_you_names_what_an_incident_affects_not_who_detected_it():
    db = _session()
    _company(db)  # INC-0042 is detected by the watchdog, no service linked
    svc = db.query(models.Service).one()
    inc = db.query(models.Incident).one()
    inc.severity, inc.service_id = enums.IncidentSeverity.SEV1, svc.id
    db.commit()
    [item] = [i for i in _state(db)["needs_you"] if i["kind"] == "SEV1"]
    assert item["affected"] == "Seerxo · Seerxo API"


def _migrated(db, rev):
    from sqlalchemy import text
    db.execute(text("create table alembic_version (version_num varchar(32))"))
    db.execute(text("insert into alembic_version values (:r)"), {"r": rev})


def test_runtime_shows_what_is_live_and_flags_every_mismatch(monkeypatch):
    db = _session()
    head = desk._code_migration_head()
    assert head  # the image's own migrations resolve
    _migrated(db, head)
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", "a" * 40)
    rt = desk.runtime_view(db, {"runtime": {"git_sha": "a" * 40, "image": "w", "started_at": "t"},
                                "autonomy": {"code_write_flag": False}})
    assert rt["drift"] == [] and rt["migration"] == {"database": head, "code_head": head}
    assert rt["stage_c_enabled"] is False and rt["guardian_enabled"] is False
    # The reconciler runs in this (API) process via the watchdog, so its switch is read here:
    # an operator must not have to inspect container env to learn goal planning is unattended.
    monkeypatch.setenv("RECONCILER_AUTONOMOUS_PLANNING", "true")
    assert desk.runtime_view(db, {})["reconciler_autonomous_planning"] is True
    monkeypatch.delenv("RECONCILER_AUTONOMOUS_PLANNING")
    assert desk.runtime_view(db, {})["reconciler_autonomous_planning"] is False
    # Break caught: a half-finished deploy (api and worker on different code, schema behind)
    # reads as healthy.
    db.execute(__import__("sqlalchemy").text("update alembic_version set version_num = 'old'"))
    rt = desk.runtime_view(db, {"runtime": {"git_sha": "b" * 40}})
    assert len(rt["drift"]) == 2 and "aaaaaaa" in rt["drift"][0] and "bbbbbbb" in rt["drift"][0]


def test_unknown_provenance_is_drift_not_a_match(monkeypatch):
    db = _session()
    monkeypatch.delenv("COMPANY_OS_GIT_SHA", raising=False)
    rt = desk.runtime_view(db, {})
    # No SHA anywhere and no alembic table: both reported, never silently equal.
    assert any("unknown" in d for d in rt["drift"]) and rt["migration"]["database"] is None
    assert len(rt["drift"]) == 2


def test_desk_state_carries_runtime_and_worker_heartbeat_carries_identity():
    db = _session()
    _company(db)
    assert "drift" in _state(db)["runtime"]
    worker._heartbeat(db, runtime=worker.RUNTIME)
    assert worker.worker_state(db)["runtime"]["started_at"] == worker.RUNTIME["started_at"]


def test_unknown_migrations_never_count_as_matching(monkeypatch):
    monkeypatch.setattr(desk, "_code_migration_head", lambda: None)
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", "a" * 40)
    rt = desk.runtime_view(_session(), {"runtime": {"git_sha": "a" * 40}})
    assert rt["migration"] == {"database": None, "code_head": None}
    assert len(rt["drift"]) == 1
