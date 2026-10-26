"""A worker run that no longer holds its task never writes over it (worker.holds_attempt), on real PostgreSQL.

The founder acts through the real API (verified Access identity) on its own connection while the provider is
running, exactly as a Desk click would. Covered: cancel mid-run (success, failure and capacity outcomes), ABA
(cancel then claimed again, cancel then a manual IN_PROGRESS, stale recovery then a new claim), stale-run
recovery of a superseded run, and a founder cancel racing the worker's finalization under the row lock.

Requires EXTERNAL_RELEASE_TEST_DATABASE_URL pointing at the disposable database company_external_release_test.
Each test runs in its own schema, so it cannot collide with the other PostgreSQL suites.
"""
import os
import random
import threading
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import cloudflare_access, enums, main, models, registry, worker
from app.providers import Health, ProviderResult
from tests.test_worker import FakeProvider, _good

URL = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
SPECS = registry.load()
DESK_HOST, FOUNDER = "desk.example.invalid", "founder@example.invalid"
FOUNDER_H = {"Cf-Access-Jwt-Assertion": "founder-assertion"}
KEY = "TASK-0100"
T = enums.TaskStatus


@pytest.fixture
def engine(monkeypatch):
    if not URL:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(URL).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    schema = f"own_{uuid.uuid4().hex[:12]}"
    admin = create_engine(URL)
    with admin.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    url = make_url(URL).update_query_dict({"options": f"-csearch_path={schema}"})
    eng = create_engine(url, pool_size=8)
    models.Base.metadata.create_all(eng)
    with Session(eng) as db:
        for key in ("ceo", "developer", "supervisor", "research"):
            db.add(models.Agent(key=key, role=key, enabled=True))
        db.commit()
    for k, v in {"COMPANY_API_TOKEN_ADMIN": "a" * 40, "COMPANY_API_TOKEN_DESK": "d" * 40,
                 "COMPANY_API_TOKEN_YOUTUBE": "y" * 40, "CF_ACCESS_DESK_HOST": DESK_HOST,
                 "API_AUTH_MODE": "enforce"}.items():
        monkeypatch.setenv(k, v)

    async def identity(token, **kw):
        return FOUNDER if token == "founder-assertion" else None
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)

    def get_db():
        with Session(eng) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db
    try:
        yield eng
    finally:
        main.app.dependency_overrides.clear()
        eng.dispose()
        with admin.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.dispose()


def _task(engine, key=KEY):
    with Session(engine) as db:
        research = db.scalar(select(models.Agent).where(models.Agent.key == "research"))
        db.add(models.Task(key=key, title="probe", success_criteria="evidence has token",
                           objective="Echo the verification token", status=T.READY, priority=enums.Priority.P3,
                           risk_level=enums.RiskLevel.LOW, assigned_agent_id=research.id,
                           context={"autonomous": True, "scope": "company-os",
                                    "verify": {"must_contain": ["selftest-x", key]}}))
        db.commit()
    return key


def _founder(status, key=KEY, **extra):
    """A Desk click: the founder's verified Access identity, its own connection and transaction."""
    r = TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000)).post(
        f"/tasks/{key}/status", headers=FOUNDER_H, json={"status": status, "reason": "founder from the Desk", **extra})
    assert r.status_code == 200, r.text


class During(FakeProvider):
    """A provider that lets something happen while the model 'runs', then answers."""

    def __init__(self, outputs, during):
        super().__init__("claude", outputs)
        self.during = during

    def _run(self, prompt, schema, **kw):
        self.during()
        return super()._run(prompt, schema, **kw)


def _tick(engine, provider):
    with Session(engine) as db:
        return worker.run_once(db, {"claude": provider}, SPECS)


def _row(engine, key=KEY):
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == key))
        runs = db.scalars(select(models.AgentRun).where(models.AgentRun.task_id == t.id).order_by(models.AgentRun.id)).all()
        actions = [a.action for a in db.scalars(select(models.AuditLog).where(models.AuditLog.task_id == t.id)
                                                .order_by(models.AuditLog.id))]
        children = db.scalar(select(func.count()).select_from(models.Task).where(models.Task.key != key))
        notes = db.scalar(select(func.count()).select_from(models.AgentNote))
        return t, runs, actions, children, notes


def _assert_superseded(run, actions):
    assert run.status == enums.AgentRunStatus.FAILED and run.error.startswith("superseded:")
    assert run.supervisor_verdict is None and run.result is None
    assert "result_checkpoint" not in (run.tool_calls or {}) and run.tool_calls.get("superseded") is True
    assert actions[-1] == "task.autonomous-run.superseded"


GOOD = _good(KEY, learning_candidates=["a lesson"],
             new_task_candidates=[{"title": "Analyze the measured audience", "reason": "follow-up", "kind": "research"}])


@pytest.mark.parametrize("outcome", ["success", "failure", "capacity"])
def test_a_cancel_during_the_run_is_never_overwritten(engine, outcome):
    _task(engine)
    answer = {"success": GOOD, "failure": {"nonsense": True},
              "capacity": ProviderResult("claude", Health.QUOTA_EXHAUSTED, error="usage limit")}[outcome]
    out = _tick(engine, During([answer], lambda: _founder("CANCELLED")))
    t, runs, actions, children, notes = _row(engine)
    assert out["verdict"] == "SUPERSEDED"
    assert t.status == T.CANCELLED and t.retry_count == 0 and t.result is None
    assert children == 0 and notes == 0            # no child task, no note from a discarded answer
    _assert_superseded(runs[-1], actions)
    assert "task.autonomous-run" not in actions    # never recorded as a finished autonomous run


def _requeue_past_open_run(engine):
    """Re-queue while the old run is still open. The founder's READY is refused then (worker.ready_refusal: a second
    run would start), so the re-queue is modelled directly in the database, as another path could do; the old run's
    answer must still never get through."""
    r = TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000)).post(
        f"/tasks/{KEY}/status", headers=FOUNDER_H, json={"status": "READY", "reason": "founder from the Desk"})
    assert r.status_code == 409 and "still open" in r.json()["detail"], r.text
    with Session(engine) as db:
        db.execute(update(models.Task).where(models.Task.key == KEY).values(status=T.READY))
        db.commit()


def test_aba_cancelled_then_claimed_again_rejects_the_old_result(engine):
    _task(engine)

    def cancel_and_reclaim():
        _founder("CANCELLED")
        _requeue_past_open_run(engine)              # released again while the old run is still in flight
        with Session(engine) as db:                 # a second worker claims it before the old run ends
            assert worker.claim(db, db.scalar(select(models.Task).where(models.Task.key == KEY)))
    out = _tick(engine, During([GOOD], cancel_and_reclaim))
    t, runs, actions, children, _ = _row(engine)
    assert out["verdict"] == "SUPERSEDED"
    assert t.status == T.IN_PROGRESS and t.result is None and children == 0   # the new claim is untouched
    assert len(runs) == 1
    _assert_superseded(runs[0], actions)
    with Session(engine) as db:                     # and the new claim still holds the task
        assert worker.holds_attempt(db, worker._locked_task(db, t.id))


def test_aba_cancelled_then_set_in_progress_by_hand_rejects_the_old_result(engine):
    _task(engine)

    def cancel_and_take_over():
        _founder("CANCELLED")
        _founder("IN_PROGRESS")
    out = _tick(engine, During([GOOD], cancel_and_take_over))
    t, runs, actions, _, _ = _row(engine)
    assert out["verdict"] == "SUPERSEDED" and t.status == T.IN_PROGRESS and t.result is None
    _assert_superseded(runs[0], actions)


def test_a_same_status_takeover_is_a_takeover(engine):
    _task(engine)
    out = _tick(engine, During([GOOD], lambda: _founder("IN_PROGRESS")))
    t, runs, actions, _, _ = _row(engine)
    assert out["verdict"] == "SUPERSEDED" and t.status == T.IN_PROGRESS
    _assert_superseded(runs[0], actions)


def test_aba_stale_recovery_then_a_new_claim_rejects_the_old_result(engine):
    """Another worker decided this run was lost (recover_stale), requeued the task and claimed it again:
    status, agent and owner version are back to what the old run started with; only the claim differs."""
    _task(engine)

    def recovered_elsewhere():
        with Session(engine) as db:
            run = db.scalar(select(models.AgentRun).order_by(models.AgentRun.id.desc()))
            run.started_at -= worker.STALE_AFTER + timedelta(minutes=1)
            db.commit()
            assert worker.recover_stale(db) == [KEY]
            task = db.scalar(select(models.Task).where(models.Task.key == KEY))
            assert task.status == T.READY and task.retry_count == 1
            assert worker.claim(db, task)
    out = _tick(engine, During([GOOD], recovered_elsewhere))
    t, runs, actions, children, _ = _row(engine)
    assert out is not None and t.status == T.IN_PROGRESS and t.result is None and children == 0
    assert t.retry_count == 1                        # the recovery's retry; the old answer adds nothing
    assert runs[0].status == enums.AgentRunStatus.FAILED and "task.autonomous-run" not in actions


def test_stale_recovery_of_a_superseded_run_leaves_the_task_alone(engine):
    """A run lost after the founder cancelled and a new claim took the task: recovery fails the run only."""
    _task(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        assert worker.claim(db, task)
        db.add(models.AgentRun(task_id=task.id, agent_id=task.assigned_agent_id, provider="claude",
                               status=enums.AgentRunStatus.STARTED,
                               started_at=worker._now() - worker.STALE_AFTER - timedelta(minutes=1)))
        db.commit()
    _founder("CANCELLED")
    _requeue_past_open_run(engine)
    with Session(engine) as db:
        assert worker.claim(db, db.scalar(select(models.Task).where(models.Task.key == KEY)))
        assert worker.recover_stale(db) == [KEY]
    t, runs, _, _, _ = _row(engine)
    assert t.status == T.IN_PROGRESS and t.retry_count == 0     # not requeued, no retry spent
    assert runs[0].status == enums.AgentRunStatus.FAILED and runs[0].error.startswith("superseded:")
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.action == "task.autonomous-run.superseded")) == 1


def test_aba_before_the_run_exists_starts_no_run_and_calls_no_model(engine):
    """Worker A claimed, but before it inserted its run the founder cancelled and released the task and worker B
    claimed it. B's claim floor cannot see A's not-yet-existing run, so A must not start: its claim is not the
    newest (worker.owns_claim), checked under the row lock in the transaction that would insert the run."""
    _task(engine)
    with Session(engine) as a, Session(engine) as b:
        task_a = a.scalar(select(models.Task).where(models.Task.key == KEY))
        assert worker.claim(a, task_a)
        _founder("CANCELLED")
        _founder("READY")
        assert worker.claim(b, b.scalar(select(models.Task).where(models.Task.key == KEY)))
        provider = FakeProvider("claude", [GOOD])
        out = worker.execute(a, task_a, SPECS["research"], [provider])
        assert out["verdict"] == "SUPERSEDED" and out["model_calls"] == 0 and provider.calls == 0
    t, runs, actions, _, _ = _row(engine)
    assert t.status == T.IN_PROGRESS and runs == [] and "task.autonomous-run" not in actions


def test_the_successful_lifecycle_is_unchanged(engine):
    _task(engine)
    out = _tick(engine, During([GOOD], lambda: None))
    t, runs, actions, children, notes = _row(engine)
    assert out["verdict"] == "APPROVED" and t.status == T.REVIEW and children == 1 and notes == 1
    assert runs[0].status == enums.AgentRunStatus.SUCCEEDED and "task.autonomous-run.superseded" not in actions


@pytest.mark.parametrize("seed", range(12))
def test_a_cancel_racing_the_finalization_is_serialized_by_the_row_lock(engine, seed):
    """The founder's cancel and the worker's result land at the same moment. Either order is fine, a mix never
    is: the result is written only if it committed before the cancel; the cancel is never lost. Outcomes seen:
    APPROVED (result first), SUPERSEDED (cancel first) and RESULT_SAVED (lock busy; recovery then discards it)."""
    rnd = random.Random(seed)
    _task(engine)
    go = threading.Barrier(2)
    errors = []

    def founder():
        try:
            go.wait(timeout=10)
            time.sleep(rnd.random() * 0.02)
            _founder("CANCELLED")
        except Exception as e:  # surfaced below; a thread cannot fail the test by itself
            errors.append(e)
    thread = threading.Thread(target=founder)
    thread.start()
    out = _tick(engine, During([GOOD], lambda: (go.wait(timeout=10), time.sleep(rnd.random() * 0.02))))
    thread.join(timeout=30)
    assert not errors and not thread.is_alive()
    if out["verdict"] == "RESULT_SAVED":
        # The founder held the row lock when the worker tried to finalize, so the answer stayed saved (skip_locked).
        # The next recovery pass finalizes it, after the cancel: it must be discarded, not delivered.
        with Session(engine) as db:
            run_id = db.scalar(select(models.AgentRun.id))
            out = worker._resume_result(db, run_id, SPECS)
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == KEY))
        rows = db.scalars(select(models.AuditLog).where(models.AuditLog.task_id == t.id).order_by(models.AuditLog.id)).all()
        run = db.scalar(select(models.AgentRun).where(models.AgentRun.task_id == t.id))
        cancel = next(a.id for a in rows if a.action == "task.status" and a.after.get("status") == "CANCELLED")
        assert t.status == T.CANCELLED                                   # the cancel always lands
        if out["verdict"] == "SUPERSEDED":
            assert run.status == enums.AgentRunStatus.FAILED and not any(a.action == "task.autonomous-run" for a in rows)
            assert next(a.id for a in rows if a.action == "task.autonomous-run.superseded") > cancel
        else:
            finished = next(a for a in rows if a.action == "task.autonomous-run")
            assert out["verdict"] == "APPROVED" and run.status == enums.AgentRunStatus.SUCCEEDED
            assert finished.id < cancel and finished.after["status"] == "REVIEW"   # the result committed first
            assert next(a for a in rows if a.id == cancel).before["status"] == "REVIEW"
