"""A provider outage holds its own tasks without starving unrelated eligible work."""
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from app import control, enums, models, worker
from app.providers import Health
from tests.test_worker import SPECS, FakeProvider, _db, _good, _task


def _queue(db):
    research = _task(db, key="TASK-8200", priority=enums.Priority.P1)
    ceo = db.query(models.Agent).filter_by(key="ceo").one()
    planning = _task(db, key="TASK-8201", priority=enums.Priority.P2, assigned_agent_id=ceo.id)
    specs = {**SPECS, "research": replace(SPECS["research"], providers=["claude"]),
             "ceo": replace(SPECS["ceo"], providers=["codex"])}
    return research, planning, specs


def test_an_unavailable_first_provider_does_not_starve_another_agents_ready_task():
    db = _db()
    research, planning, specs = _queue(db)
    unavailable = FakeProvider("claude", [], health=Health.QUOTA_EXHAUSTED)
    available = FakeProvider("codex", [_good(planning.key, agent_id="ceo")])

    outcome = worker.run_once(db, {"claude": unavailable, "codex": available}, specs)

    assert outcome.get("task") == planning.key and outcome["verdict"] == "APPROVED"
    assert research.status == enums.TaskStatus.READY and research.retry_count == 0
    assert planning.status == enums.TaskStatus.REVIEW and available.calls == 1 and unavailable.calls == 0
    assert {"task": research.key, "reason": "provider_unavailable"} in outcome["skipped"]


def test_an_unavailable_queue_is_bounded_and_makes_no_model_call():
    db = _db()
    research, planning, specs = _queue(db)
    unavailable = {name: FakeProvider(name, [], health=Health.QUOTA_EXHAUSTED) for name in ("claude", "codex")}

    outcome = worker.run_once(db, unavailable, specs)

    assert outcome["model_calls"] == 0 and all(p.calls == 0 for p in unavailable.values())
    assert research.status == planning.status == enums.TaskStatus.READY
    assert {item["task"] for item in outcome["skipped"]} == {research.key, planning.key}


def test_a_metered_provider_still_stops_the_whole_tick():
    db = _db()
    _, planning, specs = _queue(db)
    blocked = FakeProvider("claude", [], health=Health.METERED_BLOCKED)
    available = FakeProvider("codex", [_good(planning.key, agent_id="ceo")])

    outcome = worker.run_once(db, {"claude": blocked, "codex": available}, specs)

    assert outcome["blocked"] == "zero_cost_guard" and available.calls == 0
    assert planning.status == enums.TaskStatus.READY


def test_a_waiting_coder_does_not_hold_read_only_work_or_consume_its_authorization(monkeypatch, tmp_path):
    from tests.test_stage_c_worker import FOUNDER_ID, fully_accepted, runtime_report, write_task

    db = fully_accepted(monkeypatch, tmp_path)
    runtime_report(db, "a" * 40)
    monkeypatch.setattr(worker, "ENABLE_AUTONOMOUS_CODE_WRITE", True)
    write = write_task(db, priority=enums.Priority.P1)
    control.authorize_pilot(db, write.key, "a" * 40, founder=FOUNDER_ID)
    read = _task(db, key="TASK-8201", priority=enums.Priority.P2)
    claude = FakeProvider("claude", [], health=Health.QUOTA_EXHAUSTED)
    codex = FakeProvider("codex", [_good(read.key)])

    outcome = worker.run_once(db, {"claude": claude, "codex": codex}, SPECS)

    assert outcome["task"] == read.key and read.status == enums.TaskStatus.REVIEW
    assert write.status == enums.TaskStatus.READY and write.retry_count == 0
    assert {"task": write.key, "reason": "stage_c_coder_unavailable"} in outcome["skipped"]
    assert db.get(models.ControlState, control.PILOT_AUTHORIZATION).value["consumed_at"] is None


def test_many_tasks_share_one_capacity_probe_and_one_agent_alert_per_tick():
    from tests.test_worker import _events

    db = _db()
    for n in range(20):
        _task(db, key=f"TASK-{8300 + n}")

    class Unavailable(FakeProvider):
        probes = 0

        def health_check(self):
            self.probes += 1
            return super().health_check()

    provider = Unavailable("claude", [], health=Health.QUOTA_EXHAUSTED)
    outcome = worker.run_once(db, {"claude": provider}, SPECS)

    assert outcome["model_calls"] == 0 and provider.calls == 0
    assert provider.probes == 2  # heartbeat plus one cached admission-time probe
    assert len(outcome["skipped"]) == 20 and _events(db, "WORKER_UNHEALTHY") == 1


@pytest.mark.parametrize("expire_on_commit", [True, False])
def test_founder_pause_between_candidates_stops_the_tick(monkeypatch, expire_on_commit):
    db = _db()
    db.expire_on_commit = expire_on_commit
    _, planning, specs = _queue(db)
    control.set_agents_paused(db, False, actor="founder", reason="Initial state")
    cached_pause = db.get(models.ControlState, "agents_paused")

    def pause_on_outage(session, event):
        if event.event_type == "WORKER_UNHEALTHY":
            with Session(db.get_bind()) as founder:
                control.set_agents_paused(founder, True, actor="founder", reason="Pause queue")

    monkeypatch.setattr(worker.notifier, "dispatch", pause_on_outage)
    claude = FakeProvider("claude", [], health=Health.QUOTA_EXHAUSTED)
    codex = FakeProvider("codex", [_good(planning.key, agent_id="ceo")])
    outcome = worker.run_once(db, {"claude": claude, "codex": codex}, specs)

    assert outcome["paused"] is True and outcome["model_calls"] == 0 and codex.calls == 0
    assert cached_pause.value["paused"] is True
    assert planning.status == enums.TaskStatus.READY


def test_pause_during_capacity_check_stops_before_claim(monkeypatch):
    db = _db()
    db.expire_on_commit = False
    task = _task(db)

    class Pausing(FakeProvider):
        probes = 0

        def health_check(self):
            self.probes += 1
            if self.probes == 2:
                with Session(db.get_bind()) as founder:
                    control.set_agents_paused(founder, True, actor="founder", reason="Pause before claim")
            return super().health_check()

    provider = Pausing("claude", [_good(task.key)])
    outcome = worker.run_once(db, {"claude": provider}, SPECS)

    assert outcome["paused"] is True and provider.calls == 0
    assert task.status == enums.TaskStatus.READY
