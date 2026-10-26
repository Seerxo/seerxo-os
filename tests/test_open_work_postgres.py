"""Concurrent writers against real PostgreSQL: open work and keys stay unique (§150).

Same guard as test_external_release_postgres: only the disposable CI database is allowed.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier, Event
from threading import Lock
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import enums, keys, models

WRITERS = 4

FOUNDER_ID = "founder:access:founder@example.invalid"


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url, pool_size=WRITERS + 2)
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _race(engine, build, prefix):
    """WRITERS sessions insert at the same instant; returns (keys created, duplicates)."""
    gate = Barrier(WRITERS)

    def one(_):
        with Session(engine) as db:
            obj = build()
            gate.wait()
            try:
                keys.add_with_key(db, obj, prefix)
                db.commit()
                return obj.key
            except keys.DuplicateOpenWork:
                db.rollback()
                return None

    with ThreadPoolExecutor(WRITERS) as pool:
        out = list(pool.map(one, range(WRITERS)))
    return [k for k in out if k], out.count(None)


def _task(dedupe):
    return models.Task(title="race", success_criteria="s", status=enums.TaskStatus.READY,
                       priority=enums.Priority.P2, risk_level=enums.RiskLevel.LOW, dedupe_key=dedupe)


def test_concurrent_identical_work_commits_exactly_once(engine):
    dedupe = f"race:{uuid.uuid4()}"
    created, duplicates = _race(engine, lambda: _task(dedupe), "TASK")
    assert len(created) == 1 and duplicates == WRITERS - 1
    with Session(engine) as db:
        assert db.execute(select(func.count()).select_from(models.Task)
                          .where(models.Task.dedupe_key == dedupe)).scalar_one() == 1


def test_concurrent_distinct_work_all_commits_with_unique_keys(engine):
    # Every writer computes the same next key; collisions must retry, not fail or duplicate.
    created, duplicates = _race(engine, lambda: _task(None), "TASK")
    assert duplicates == 0 and len(created) == WRITERS and len(set(created)) == WRITERS


def test_concurrent_incidents_for_one_scope_open_once(engine):
    scope = f"service:{uuid.uuid4()}"

    def incident():
        return models.Incident(severity=enums.IncidentSeverity.SEV1, title="race", description="d",
                               started_at=datetime.now(timezone.utc), detected_by="race",
                               open_scope=scope)

    created, duplicates = _race(engine, incident, "INC")
    assert len(created) == 1 and duplicates == WRITERS - 1


def test_concurrent_critical_notifications_send_once(engine):
    from app import notify

    class Adapter:
        configured = True

        def __init__(self):
            self.sent = 0
            self.lock = Lock()

        def send(self, _text):
            with self.lock:
                self.sent += 1

    adapter = Adapter()
    notifier = notify.NotificationService(adapter)
    source = f"notification-race-{uuid.uuid4().hex}"
    gate = Barrier(WRITERS)

    def one(_):
        with Session(engine) as db:
            event = models.Event(event_type="SERVICE_DOWN", source=source,
                                 severity=enums.Severity.HIGH, payload={})
            db.add(event)
            db.commit()
            gate.wait()
            return notifier.dispatch(db, event)[0]

    with ThreadPoolExecutor(WRITERS) as pool:
        results = list(pool.map(one, range(WRITERS)))
    assert results.count(True) == 1
    assert adapter.sent == 1


def test_concurrent_reports_of_one_measurement_store_one_row(engine):
    from app import metrics_store, schemas
    with Session(engine) as db:
        unit = db.execute(select(models.BusinessUnit).where(models.BusinessUnit.key == "pg-race")).scalar_one_or_none()
        if unit is None:
            db.add(models.BusinessUnit(key="pg-race", name="race", status=enums.BusinessUnitStatus.ACTIVE,
                                       priority=enums.Priority.P2))
            db.commit()
    subject = f"youtube:{uuid.uuid4().hex[:11]}"
    body = schemas.MetricsIn(business_unit="pg-race", observations=[schemas.MetricIn(
        subject=subject, metric="views", window="24h", value=42, source="youtube-data-api",
        observed_at=datetime.now(timezone.utc))])
    gate = Barrier(WRITERS)

    def one(_):
        with Session(engine) as db:
            gate.wait()
            return metrics_store.record(db, body)

    with ThreadPoolExecutor(WRITERS) as pool:
        results = list(pool.map(one, range(WRITERS)))
    assert sum(r["inserted"] for r in results) == 1
    assert sum(r["unchanged"] for r in results) == WRITERS - 1
    with Session(engine) as db:
        assert db.execute(select(func.count()).select_from(models.MetricObservation)
                          .where(models.MetricObservation.subject == subject)).scalar_one() == 1


def test_metric_conflicts_and_numeric_precision_on_postgres(engine):
    from app import metrics_store, schemas
    with Session(engine) as db:
        for key in ("pg-race", "pg-other"):
            if db.scalar(select(models.BusinessUnit).where(models.BusinessUnit.key == key)) is None:
                db.add(models.BusinessUnit(key=key, name=key, status=enums.BusinessUnitStatus.ACTIVE,
                                           priority=enums.Priority.P2))
        db.commit()

    subject = f"youtube:{uuid.uuid4().hex[:11]}"
    observed = datetime.now(timezone.utc)

    def reading(value, unit="views", business_unit="pg-race"):
        return schemas.MetricsIn(business_unit=business_unit, observations=[schemas.MetricIn(
            subject=subject, metric="views", window="24h", value=value, unit=unit,
            source="youtube-data-api", observed_at=observed)])

    with Session(engine) as db:
        assert metrics_store.record(db, reading(1.23459))["inserted"] == 1
        assert metrics_store.record(db, reading(1.2346))["unchanged"] == 1
        for conflicting in (reading(1.2347), reading(1.2346, unit="clicks"),
                            reading(1.2346, business_unit="pg-other")):
            result = metrics_store.record(db, conflicting)
            assert result["inserted"] == result["unchanged"] == 0
            assert len(result["rejected"]) == 1 and "conflicting reading" in result["rejected"][0]
        rows = db.scalars(select(models.MetricObservation).where(models.MetricObservation.subject == subject)).all()
        assert len(rows) == 1 and float(rows[0].value) == 1.2346 and rows[0].unit == "views"


def test_concurrent_guardians_checkpoint_and_create_one_finding(engine):
    from app import repo_guardian
    token = uuid.uuid4().hex
    repository = f"fixture/repo-{token}"
    project_key = f"guardian-{token}"
    with Session(engine) as db:
        agent = db.scalar(select(models.Agent).where(models.Agent.key == "developer"))
        if agent is None:
            db.add(models.Agent(key="developer", role="developer", enabled=True))
        else:
            agent.enabled = True
        db.add(models.Project(key=project_key, name="Guardian fixture", repository=repository,
                              status=enums.ProjectStatus.ACTIVE))
        db.commit()
    now = datetime.now(timezone.utc)
    facts = {"repository": repository, "main_sha": "a" * 40,
             "observed_at": now.isoformat(), "checks_complete": True,
             "checks": [{"id": 1, "name": "ci", "head_sha": "a" * 40,
                         "status": "completed", "conclusion": "failure"}]}
    gate = Barrier(WRITERS)

    def one(_):
        with Session(engine) as db:
            # Avoid hanging a merge gate if checkpoint/key locks regress.
            db.connection().exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            gate.wait()
            return repo_guardian.reconcile(db, project_key, facts, enabled=True,
                                           allowlist={repository}, now=now)

    with ThreadPoolExecutor(WRITERS) as pool:
        results = list(pool.map(one, range(WRITERS)))
    assert sum(r["created"] for r in results) == 1
    with Session(engine) as db:
        project = db.scalar(select(models.Project).where(models.Project.key == project_key))
        assert db.scalar(select(func.count()).select_from(models.Task).where(models.Task.project_id == project.id)) == 1
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.project_id == project.id, models.Event.event_type == "REPOSITORY_GUARDIAN_FINDING")) == 1
        checkpoint = db.get(models.ControlState, repo_guardian.checkpoint_key(repository))
        assert checkpoint.value["last_seen_main_sha"] == "a" * 40


def test_other_repository_progresses_while_one_checkpoint_is_locked(engine):
    from app import repo_guardian
    token = uuid.uuid4().hex
    repositories = [f"fixture/repo-{token}-{n}" for n in range(2)]
    with Session(engine) as db:
        for n, repository in enumerate(repositories):
            db.add(models.Project(key=f"guardian-{token}-{n}", name="Guardian fixture", repository=repository,
                                  status=enums.ProjectStatus.ACTIVE))
            db.add(models.ControlState(key=repo_guardian.checkpoint_key(repository), value={"repository": repository}, updated_by="test"))
        db.commit()
    with Session(engine) as holder, Session(engine) as other:
        holder.scalar(select(models.ControlState).where(
            models.ControlState.key == repo_guardian.checkpoint_key(repositories[0])).with_for_update())
        other.connection().exec_driver_sql("SET LOCAL lock_timeout = '1s'")
        facts = {"repository": repositories[1], "main_sha": "b" * 40,
                 "observed_at": datetime.now(timezone.utc).isoformat()}
        out = repo_guardian.reconcile(other, f"guardian-{token}-1", facts, enabled=True, allowlist=set(repositories))
        assert out["created"] == 0 and "skipped" not in out
        assert other.get(models.ControlState, repo_guardian.checkpoint_key(repositories[1])).value["last_seen_main_sha"] == "b" * 40


@pytest.mark.parametrize("change", ["repository", "business_unit"])
def test_guardian_rechecks_project_scope_after_real_lock_wait(engine, monkeypatch, change):
    from app import repo_guardian
    token = uuid.uuid4().hex
    repository = f"fixture/repo-{token}"
    project_key = f"guardian-{token}"
    with Session(engine) as db:
        unit = models.BusinessUnit(key=f"paused-{token}", name="Paused fixture",
                                   status=enums.BusinessUnitStatus.PAUSED)
        db.add(unit)
        db.flush()
        unit_id = unit.id
        db.add(models.Project(key=project_key, name="Guardian fixture", repository=repository,
                              status=enums.ProjectStatus.ACTIVE))
        db.add(models.ControlState(key=repo_guardian.checkpoint_key(repository), value={"repository": repository}, updated_by="test"))
        db.commit()
    reached = Event()
    original = repo_guardian._locked_checkpoint
    def reached_lock(db, repo):
        reached.set()
        return original(db, repo)
    monkeypatch.setattr(repo_guardian, "_locked_checkpoint", reached_lock)
    facts = {"repository": repository, "main_sha": "c" * 40,
             "observed_at": datetime.now(timezone.utc).isoformat(),
             "checks": [{"id": 2, "name": "ci", "head_sha": "c" * 40,
                         "status": "completed", "conclusion": "failure"}]}
    def observe():
        with Session(engine) as db:
            db.connection().exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            return repo_guardian.reconcile(db, project_key, facts, enabled=True, allowlist={repository})
    with Session(engine) as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.scalar(select(models.ControlState).where(
            models.ControlState.key == repo_guardian.checkpoint_key(repository)).with_for_update())
        future = pool.submit(observe)
        try:
            assert reached.wait(2)
            with Session(engine) as editor:
                project = editor.scalar(select(models.Project).where(models.Project.key == project_key))
                if change == "repository":
                    project.repository = "fixture/changed-repository"
                else:
                    project.business_unit_id = unit_id
                editor.commit()
        finally:
            holder.rollback()
        assert future.result(timeout=5)["skipped"] == ("project_scope_changed" if change == "repository" else "business_unit_not_active")
    with Session(engine) as db:
        project = db.scalar(select(models.Project).where(models.Project.key == project_key))
        assert db.scalar(select(func.count()).select_from(models.Task).where(models.Task.project_id == project.id)) == 0


def test_concurrent_lease_acquire_grants_exactly_one_holder(engine):
    from app import stage_c
    repository = f"fixture/lease-{uuid.uuid4().hex}"
    with Session(engine) as db:  # existing row: the race is on the row lock, not the insert
        db.add(models.ControlState(key=stage_c.lease_key(repository), value={"token": 0},
                                   updated_by="test"))
        db.commit()
    gate = Barrier(WRITERS)

    def one(i):
        with Session(engine) as db:
            db.connection().exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            gate.wait()
            token = stage_c.acquire(db, repository, f"TASK-{i}", timedelta(minutes=30))
            db.commit()
            return token

    with ThreadPoolExecutor(WRITERS) as pool:
        tokens = [t for t in pool.map(one, range(WRITERS)) if t is not None]
    # Break caught: two tasks both believe they own the repository and push competing branches.
    assert tokens == [1]


def test_deploy_interlock_query_runs_on_the_real_json_column(engine):
    """agent_runs.tool_calls is json, not jsonb: the deploy precheck must cast, or every deploy errors."""
    import re
    from pathlib import Path
    from sqlalchemy import text
    script = (Path(__file__).parents[1] / "scripts" / "deploy-company-os.sh").read_text()
    sql = re.search(r'STAGE_C_RUN=\$\(q "(select .*?)"\)', script).group(1).replace('\\"', '"')
    with Session(engine) as db:
        db.execute(text(sql)).scalar()  # raises on "operator does not exist: json @> jsonb"


def test_a_second_claimant_waits_on_the_row_lock_and_then_finds_the_authorization_spent(engine):
    """Deterministic race: A has spent the authorization but not committed. Without a row lock B would
    validate against the old, unspent row and also succeed; with it B blocks, then is denied."""
    from threading import Thread
    import time
    from app import control
    sha = "c" * 40
    with Session(engine) as db:
        task = _task(f"authz-{uuid.uuid4()}")
        keys.add_with_key(db, task, "TASK")
        db.commit()
        key = task.key
        control.authorize_pilot(db, key, sha, founder=FOUNDER_ID)
    result = {}

    def claimant_b():
        with Session(engine) as db:
            result["denial"] = control.consume_pilot_authorization(db, key, sha, 2)
            result["done_at"] = time.monotonic()
            db.commit()

    with Session(engine) as a:
        assert control.consume_pilot_authorization(a, key, sha, 1) is None   # A holds the row, uncommitted
        b = Thread(target=claimant_b)
        b.start()
        time.sleep(1.0)
        assert "denial" not in result, "B did not wait for A: the authorization could be spent twice"
        released = time.monotonic()
        a.commit()
    b.join(timeout=10)
    assert result["denial"] == "stage_c_authorization_consumed" and result["done_at"] >= released
    with Session(engine) as db:
        assert db.get(models.ControlState, "stage-c-pilot-authorization").value["consumed_by_run_id"] == 1


def _claimed_stale_task(engine):
    """A READY task claimed by the worker (claim audit row) whose worker then died: stale, no run."""
    from app import worker
    with Session(engine) as db:
        task = _task(f"claimrace-{uuid.uuid4()}")
        keys.add_with_key(db, task, "TASK")
        db.commit()
        assert worker.claim(db, task) is True
        task.updated_at = datetime.now(timezone.utc) - worker.STALE_AFTER - timedelta(minutes=5)
        db.commit()
        return task.id, task.key


def _in_thread(fn):
    from threading import Thread
    result = {}

    def run():
        with Session(engine_holder["engine"]) as db:
            result["value"] = fn(db)
    thread = Thread(target=run)
    thread.start()
    return thread, result


engine_holder = {}


def test_recovery_blocks_behind_a_founder_takeover_in_flight_and_then_stands_down(engine):
    """The founder is mid-update on the row (same-status takeover: no column changes, only an audit row).
    Recovery must wait for that row, then see the newer audit entry and leave the task alone. Without a
    row lock in recovery it would read the old claim row, win, and requeue the founder's task."""
    import time
    from app import audit, worker
    engine_holder["engine"] = engine
    task_id, key = _claimed_stale_task(engine)
    with Session(engine) as founder:
        task = founder.get(models.Task, task_id)
        founder.refresh(task, with_for_update=True)                          # what update_task_status now does
        audit.record(founder, actor="founder", action="task.status", entity_type="task", entity_key=key,
                     reason="taking over", before={"status": "IN_PROGRESS"}, after={"status": "IN_PROGRESS"},
                     task_id=task_id)
        thread, result = _in_thread(lambda db: worker._requeue_unstarted_claim(db, task_id))
        time.sleep(1.0)
        assert "value" not in result, "recovery did not wait for the founder's row lock"
        founder.commit()
    thread.join(timeout=10)
    assert result["value"] is False
    with Session(engine) as db:
        assert db.get(models.Task, task_id).status == enums.TaskStatus.IN_PROGRESS
        assert db.query(models.AuditLog).filter_by(task_id=task_id, action="task.claim.recovered").count() == 0


def test_a_founder_status_update_blocks_behind_recovery_in_flight_and_sees_its_result(engine):
    """Recovery holds the row. The real update_task_status (same-status request) must wait on the same
    lock, then act on the READY task instead of the stale IN_PROGRESS it would otherwise have read."""
    import time
    from app import audit, main, schemas
    engine_holder["engine"] = engine
    task_id, key = _claimed_stale_task(engine)
    seen = {}

    def founder_update(db):
        out = main.update_task_status(key, schemas.TaskStatusUpdate(
            status=enums.TaskStatus.IN_PROGRESS, actor="founder", reason="taking over"), db)
        seen["before"] = db.query(models.AuditLog).filter_by(task_id=task_id, action="task.status").one().before
        return out.status

    with Session(engine) as recovery:
        task = recovery.scalar(select(models.Task).where(models.Task.id == task_id).with_for_update())
        task.status = enums.TaskStatus.READY                                  # what recovery does under its lock
        audit.record(recovery, actor="company-worker", action="task.claim.recovered", entity_type="task",
                     entity_key=key, before={"status": "IN_PROGRESS"}, after={"status": "READY"}, task_id=task_id)
        thread, result = _in_thread(founder_update)
        time.sleep(1.0)
        assert "value" not in result, "the founder's status update did not wait for the row lock"
        recovery.commit()
    thread.join(timeout=10)
    assert result["value"] == enums.TaskStatus.IN_PROGRESS                    # the founder's decision stands, applied last
    assert seen["before"]["status"] == "READY"                                # and it saw recovery's result
