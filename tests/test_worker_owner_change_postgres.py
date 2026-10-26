"""D1/D2 on real PostgreSQL: a genuine ownership change is told apart from an ownership transfer request, a newer
claim and a founder cancellation. Each actor uses its own connection while the provider runs.

  * real assigned_agent_id change (out of band, e.g. an operator) -> BLOCKED + TASK_BLOCKED, never READY;
  * ownership transfer through the API while IN_PROGRESS -> 409, nothing changes, the attempt finishes;
  * a newer claim -> the old result is rejected, the new claim is untouched, no block;
  * founder cancellation -> CANCELLED stands, no block.

Uses the schema-isolated fixtures of tests/test_worker_ownership_postgres.py.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, main, models, worker
from tests.test_worker_ownership_postgres import (DESK_HOST, FOUNDER_H, KEY, T, During, _founder, _requeue_past_open_run, _row, _task,
                                                  _tick, engine)  # noqa: F401  (engine is a fixture)
from tests.test_worker import _good

GOOD = _good(KEY)


def _blocked_events(engine):
    with Session(engine) as db:
        return list(db.scalars(select(models.Event.event_type).where(models.Event.event_type == "TASK_BLOCKED")))


def _reassign_out_of_band(engine, agent_key="ceo", *, audited=True):
    with Session(engine) as db:
        t = db.scalar(select(models.Task).where(models.Task.key == KEY).with_for_update())
        t.assigned_agent_id = db.scalar(select(models.Agent.id).where(models.Agent.key == agent_key))
        if audited:
            db.add(models.AuditLog(actor="operator:local", action="task.assign", entity_type="task", entity_key=KEY,
                                   task_id=t.id))
        db.commit()


@pytest.mark.parametrize("audited", [True, False])
def test_a_real_agent_change_mid_run_blocks_with_an_event_and_is_never_requeued(engine, monkeypatch, audited):
    _task(engine)
    out = _tick(engine, During([GOOD], lambda: _reassign_out_of_band(engine, audited=audited)))
    t, runs, actions, children, _ = _row(engine)
    assert out["verdict"] == "SUPERSEDED"
    assert (t.status, t.error, t.result, children) == (T.BLOCKED, worker.OWNER_CHANGED_BLOCK, None, 0)
    assert runs[0].status == enums.AgentRunStatus.FAILED and _blocked_events(engine) == ["TASK_BLOCKED"]
    later = worker._now() + worker.STALE_AFTER * 5
    monkeypatch.setattr(worker, "_now", lambda: later)
    with Session(engine) as db:
        worker.recover_stale(db)
    assert _row(engine)[0].status == T.BLOCKED


def test_an_ownership_transfer_while_in_progress_is_refused_and_the_attempt_finishes(engine):
    _task(engine)
    codes = []

    def transfer():
        r = TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000)).post(
            f"/tasks/{KEY}/assign", headers=FOUNDER_H, json={"agent": "ceo", "reason": "move it"})
        codes.append(r.status_code)
    out = _tick(engine, During([GOOD], transfer))
    t, runs, actions, _, _ = _row(engine)
    assert codes == [409] and "task.assign" not in actions
    assert out["verdict"] == "APPROVED" and t.status == T.REVIEW and _blocked_events(engine) == []


def test_a_newer_claim_is_never_blocked_by_the_old_run_even_after_a_reassignment(engine):
    _task(engine)

    def reassign_requeue_reclaim():
        _founder("CANCELLED")
        _reassign_out_of_band(engine)
        _requeue_past_open_run(engine)
        with Session(engine) as db:
            assert worker.claim(db, db.scalar(select(models.Task).where(models.Task.key == KEY)))
    out = _tick(engine, During([GOOD], reassign_requeue_reclaim))
    t, _, _, _, _ = _row(engine)
    assert out["verdict"] == "SUPERSEDED" and t.status == T.IN_PROGRESS and t.result is None
    assert _blocked_events(engine) == []
    with Session(engine) as db:
        assert worker.holds_attempt(db, worker._locked_task(db, t.id))   # the new claim still holds it


def test_a_founder_cancellation_with_a_reassignment_stays_cancelled(engine):
    _task(engine)

    def cancel_and_reassign():
        _reassign_out_of_band(engine)
        _founder("CANCELLED")
    out = _tick(engine, During([GOOD], cancel_and_reassign))
    t, _, _, _, _ = _row(engine)
    assert out["verdict"] == "SUPERSEDED" and t.status == T.CANCELLED and t.result is None
    assert _blocked_events(engine) == []
