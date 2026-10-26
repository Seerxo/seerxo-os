"""Crash recovery of the unattended worker, proven with real SIGKILLs on a disposable PostgreSQL.

Each scenario runs worker.run_once in a child process (tests/worker_crash_child.py) and kills it with SIGKILL
at a chosen Session.commit or right after the provider returns. A second child then runs recovery after a
simulated restart. The restart is modelled, not slept: timestamps are moved past the stale window, and a saved
result lease is moved into the past only when the scenario says the lease has expired. Invariants are checked
against PostgreSQL; model calls are counted from a file that survives the kill.

Requires EXTERNAL_RELEASE_TEST_DATABASE_URL pointing at the disposable database company_external_release_test.
"""
import json
import os
import pathlib
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import enums, models

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "worker_crash_child.py"
URL = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="disposable PostgreSQL URL not supplied")
KEY = "TASK-0100"


@pytest.fixture(autouse=True)
def _isolated_schema():
    """Each test gets its own PostgreSQL schema, so concurrent runs against the same database cannot collide."""
    global URL
    base = URL
    schema = f"crash_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    admin.dispose()
    URL = make_url(base).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    try:
        yield
    finally:
        URL = base
        admin = create_engine(base, future=True)
        with admin.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.dispose()


def _good(key=KEY, candidates=()):
    return {"task_id": key, "agent_id": "research", "status": "completed",
            "summary": f"echoed selftest token for {key}", "actions": [],
            "evidence": [f"token selftest-x and {key}"], "artifacts": [],
            "learning_candidates": [], "new_task_candidates": list(candidates),
            "approval_request": None, "error": None}


CHILD_CANDIDATE = {"title": "Follow-up research", "reason": "probe", "kind": "research"}


def _reset_and_seed(engine):
    assert make_url(str(engine.url)).database == "company_external_release_test", "disposable database only"
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        for name in ("ceo", "developer", "supervisor", "research"):
            db.add(models.Agent(key=name, role=name, enabled=True))
        db.commit()
        research = db.execute(select(models.Agent).where(models.Agent.key == "research")).scalar_one()
        db.add(models.Task(key=KEY, title="probe", success_criteria="evidence has token",
                           objective="Echo the verification token", status=enums.TaskStatus.READY,
                           priority=enums.Priority.P3, risk_level=enums.RiskLevel.LOW,
                           assigned_agent_id=research.id,
                           context={"autonomous": True, "scope": "company-os",
                                    "verify": {"must_contain": ["selftest-x", KEY]}}))
        db.commit()


def _child(tmp, *, crash_at=0, phase="commit_before", outputs=None):
    (tmp / "outputs.json").write_text(json.dumps(outputs or [_good(), _good()]))
    env = {**os.environ, "PYTHONPATH": str(ROOT), "DATABASE_URL": URL, "PROVIDER": "claude",
           "CRASH_AT": str(crash_at), "CRASH_PHASE": phase,
           "CALLS_FILE": str(tmp / "calls.txt"), "OUTPUTS_FILE": str(tmp / "outputs.json"),
           "RESULT_FILE": str(tmp / "result.json")}
    return subprocess.run([sys.executable, str(CHILD)], env=env, cwd=ROOT, capture_output=True,
                          text=True, timeout=180)


def _calls(tmp):
    path = tmp / "calls.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _restart(engine, *, lease_expired=True):
    """A restart after the stale window. With lease_expired the saved-result lease is also in the past."""
    past = timedelta(days=3)
    with Session(engine) as db:
        for t in db.execute(select(models.Task)).scalars():
            t.updated_at = t.updated_at - past
            if t.started_at:
                t.started_at = t.started_at - past
        for r in db.execute(select(models.AgentRun)).scalars():
            r.started_at = r.started_at - past
            cp = (r.tool_calls or {}).get("result_checkpoint")
            if r.ended_at:
                r.ended_at = r.ended_at - past  # retry backoff is measured from the last run's end
            if lease_expired and isinstance(cp, dict) and cp.get("claim_until"):
                until = datetime.fromisoformat(cp["claim_until"]) - past
                r.tool_calls = {**r.tool_calls, "result_checkpoint": {**cp, "claim_until": until.isoformat()}}
        db.commit()


def _state(engine):
    with Session(engine) as db:
        parent = db.execute(select(models.Task).where(models.Task.key == KEY)).scalar_one()
        runs = db.execute(select(models.AgentRun)).scalars().all()
        return {"status": parent.status.value, "runs": len(runs),
                "tasks": db.scalar(select(func.count()).select_from(models.Task)),
                "children": db.scalar(select(func.count()).select_from(models.Task)
                                      .where(models.Task.dedupe_key.like("candidate:%"))),
                "checkpoint": any("result_checkpoint" in (r.tool_calls or {}) for r in runs)}


def _baseline_tasks(engine, tmp_path, outputs):
    _reset_and_seed(engine)
    run_dir = tmp_path / "baseline"
    run_dir.mkdir()
    p = _child(run_dir, outputs=outputs)
    assert p.returncode == 0, p.stderr[-500:]
    return _state(engine)["tasks"]


def _sweep(tmp_path, phase, engine, outputs):
    rows, k = [], 1
    while True:
        run_dir = tmp_path / f"{phase}-{k}"
        run_dir.mkdir()
        _reset_and_seed(engine)
        child1 = _child(run_dir, crash_at=k, phase=phase, outputs=outputs)
        if child1.returncode == 0:
            break  # k is past the last commit on this path: every boundary has been covered
        assert child1.returncode == -signal.SIGKILL, (
            f"k={k}: child exited {child1.returncode} without SIGKILL: {child1.stderr[-400:]}")
        before = _state(engine)
        calls_before = _calls(run_dir)
        _restart(engine)
        child2 = _child(run_dir, outputs=outputs)  # recovery tick: finalizes, or requeues with backoff
        if _state(engine)["status"] == "READY":
            _restart(engine)  # a later tick, after the backoff has passed
            child2 = _child(run_dir, outputs=outputs)
        after = _state(engine)
        rows.append({"k": k, "killed": child1.returncode == -signal.SIGKILL, "exit2": child2.returncode,
                     "calls_before_kill": calls_before, "calls_recovery": _calls(run_dir) - calls_before,
                     "checkpoint_at_kill": before["checkpoint"], "status_at_kill": before["status"],
                     "final": after["status"], "tasks": after["tasks"], "children": after["children"]})
        k += 1
    return rows


@pytest.mark.parametrize("with_children", [False, True])
@pytest.mark.parametrize("phase", ["commit_before", "commit_after", "provider_return"])
def test_sigkill_sweep_then_recovery(tmp_path, phase, with_children):
    engine = create_engine(URL, future=True)
    # The fake provider is deterministic: a re-invoked model returns the same answer, so both calls get one output.
    answer = _good(candidates=[CHILD_CANDIDATE] if with_children else [])
    outputs = [answer, answer]
    expected_tasks = _baseline_tasks(engine, tmp_path, outputs)
    rows = _sweep(tmp_path, phase, engine, outputs)
    for r in rows:
        print("\nROW", phase, "children" if with_children else "plain", r)
    assert rows, "no kill point reached"
    assert all(r["killed"] for r in rows), "a child exited without being SIGKILLed"
    assert all(r["exit2"] == 0 for r in rows), "recovery child failed"
    assert all(r["final"] == "REVIEW" for r in rows), "a task did not reach review after recovery"
    assert all(r["tasks"] == expected_tasks for r in rows), "a task was duplicated or lost"
    assert all(r["children"] == (1 if with_children else 0) for r in rows), "a child task was not created exactly once"
    # Once the result was saved, recovery must finalize it without another model call.
    assert all(r["calls_recovery"] == 0 for r in rows if r["checkpoint_at_kill"]), "saved result re-sent to the model"


TAMPERS = {
    "output": lambda cp: {**cp, "output": {**cp["output"], "summary": "tampered"}},
    "task_hash": lambda cp: {**cp, "task_hash": "0" * 64},
    "finalizations": lambda cp: {**cp, "finalizations": 99},
}


@pytest.mark.parametrize("field", sorted(TAMPERS))
def test_corrupt_saved_result_blocks_instead_of_finalizing(tmp_path, field):
    engine = create_engine(URL, future=True)
    _reset_and_seed(engine)
    run_dir = tmp_path / "corrupt"
    run_dir.mkdir()
    child1 = _child(run_dir, crash_at=10, phase="commit_before")  # saved result exists, not yet finalized
    assert child1.returncode == -signal.SIGKILL
    with Session(engine) as db:
        run = db.execute(select(models.AgentRun)).scalars().one()
        run.tool_calls = {**run.tool_calls, "result_checkpoint": TAMPERS[field](run.tool_calls["result_checkpoint"])}
        db.commit()
    _restart(engine)
    calls_before = _calls(run_dir)
    child2 = _child(run_dir)
    assert child2.returncode == 0, child2.stderr[-500:]
    final = _state(engine)
    assert final["status"] == "BLOCKED", final
    assert _calls(run_dir) == calls_before, "a corrupt saved result caused a model call"


def test_live_lease_blocks_a_second_finalizer_until_it_expires(tmp_path):
    engine = create_engine(URL, future=True)
    _reset_and_seed(engine)
    run_dir = tmp_path / "lease"
    run_dir.mkdir()
    child1 = _child(run_dir, crash_at=12, phase="commit_before")  # finalization lease is held
    assert child1.returncode == -signal.SIGKILL
    _restart(engine, lease_expired=False)
    child2 = _child(run_dir)
    assert child2.returncode == 0, child2.stderr[-500:]
    assert _state(engine)["status"] == "IN_PROGRESS", "a second finalizer ran inside a live lease"
    _restart(engine)  # now the lease has expired too
    child3 = _child(run_dir)
    assert child3.returncode == 0, child3.stderr[-500:]
    assert _state(engine)["status"] == "REVIEW"
    assert _calls(run_dir) == 1, "the saved result was re-sent to the model"


def test_founder_pause_survives_restart_and_holds_saved_result(tmp_path):
    from app import control, registry, worker
    engine = create_engine(URL, future=True)
    _reset_and_seed(engine)
    run_dir = tmp_path / "pause"
    run_dir.mkdir()
    child1 = _child(run_dir, crash_at=10, phase="commit_before")  # result saved, not finalized
    assert child1.returncode == -signal.SIGKILL
    with Session(engine) as db:
        control.set_agents_paused(db, True, actor="founder", reason="crash-recovery probe")
        db.commit()
    # The finalizer guard itself (worker._resume_result), called in-process against the same PostgreSQL state.
    with Session(engine) as db:
        run_id = db.execute(select(models.AgentRun.id)).scalars().one()
        assert worker._resume_result(db, run_id, registry.load()) is None, "finalizer ran while paused"
        db.rollback()
    assert _state(engine)["status"] == "IN_PROGRESS"
    _restart(engine)
    calls_before = _calls(run_dir)
    child2 = _child(run_dir)
    assert child2.returncode == 0, child2.stderr[-500:]
    assert _state(engine)["status"] == "IN_PROGRESS", "a paused founder was overridden by recovery"
    assert _calls(run_dir) == calls_before, "a model call ran while paused"
    with Session(engine) as db:
        control.set_agents_paused(db, False, actor="founder", reason="crash-recovery probe")
        db.commit()
    _restart(engine)
    child3 = _child(run_dir)
    assert child3.returncode == 0, child3.stderr[-500:]
    assert _state(engine)["status"] == "REVIEW"
    assert _calls(run_dir) == calls_before, "the saved result was re-sent to the model after unpause"


def test_learning_notes_are_not_duplicated_by_recovery(tmp_path):
    engine = create_engine(URL, future=True)
    lesson = dict(_good(), learning_candidates=["lesson alpha"])
    outputs = [lesson, lesson]
    kill_points = 0
    for k in range(1, 100):
        run_dir = tmp_path / f"notes-{k}"
        run_dir.mkdir()
        _reset_and_seed(engine)
        child1 = _child(run_dir, crash_at=k, phase="commit_before", outputs=outputs)
        if child1.returncode == 0:
            break
        assert child1.returncode == -signal.SIGKILL, child1.stderr[-400:]
        kill_points += 1
        _restart(engine)
        _child(run_dir, outputs=outputs)
        if _state(engine)["status"] == "READY":
            _restart(engine)
            _child(run_dir, outputs=outputs)
        with Session(engine) as db:
            notes = db.execute(select(models.AgentNote).where(models.AgentNote.note == "lesson alpha")).scalars().all()
            merged = [(n.reference_count) for n in notes]
        assert _state(engine)["status"] == "REVIEW", k
        assert len(notes) == 1 and merged == [1], f"lesson written {len(notes)} row(s), references {merged}, kill at commit {k}"
    assert kill_points >= 10, f"only {kill_points} kill points exercised; the sweep stopped too early"
