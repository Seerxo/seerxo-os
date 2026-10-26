"""Stage C standing authorization: the founder approves named files for a few days and runs a day, once.
It widens the authority in time only. Break caught: any form of it that a non-founder can issue, that outlives a
deploy, covers a file nobody named, exceeds its daily cap, or turns on code writing by itself."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import control, models, stage_c_worker, worker
from tests.test_founder_authorization import (ASSERT, CALLERS, FID, SHA, _c, _founder, env)  # noqa: F401 (fixture)
from tests.test_stage_c_worker import (_same_repository_publishing, FOUNDER_ID, SHA_A, SHA_B, armed, assert_untouched, authorization, tick)
from tests.test_worker import SPECS

PATH = "tests/test_metrics.py"          # the file write_task() scopes its task to
STANDING = control.STANDING_AUTHORIZATION


def standing(db):
    return db.get(models.ControlState, STANDING)


def audit_actions(db):
    return [a.action for a in db.scalars(select(models.AuditLog).order_by(models.AuditLog.id))]


# ------------------------------------------------------------------------------------------- using it
def test_a_standing_authorization_runs_a_task_inside_its_scope_and_counts_the_run(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=2)
    assert control.write_authorization_denial(db, task, SHA_A) is None
    out = worker.run_once(db, ps, SPECS)
    assert out["task"] == task.key and out["run_id"]                      # claimed through normal admission
    used = standing(db).value["used"]
    assert used["count"] == 1 and used["day"] == datetime.now(timezone.utc).date().isoformat()
    assert "stage-c.standing.consume" in audit_actions(db)
    assert authorization(db) is None                                       # no one-task row was created or spent


def test_the_daily_cap_stops_further_runs_and_resets_the_next_day(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=1)
    now = datetime.now(timezone.utc)
    assert control.consume_write_authorization(db, task, SHA_A, 1, now) is None
    db.commit()
    assert control.consume_write_authorization(db, task, SHA_A, 2, now) == "stage_c_standing_daily_limit"
    assert control.write_authorization_denial(db, task, SHA_A, now) == "stage_c_standing_daily_limit"
    assert standing(db).value["used"]["count"] == 1                        # the refused attempt spent nothing
    tomorrow = now + timedelta(days=1)
    assert control.consume_write_authorization(db, task, SHA_A, 3, tomorrow) is None
    assert standing(db).value["used"] == {"day": tomorrow.date().isoformat(), "count": 1}


def test_a_one_task_authorization_is_spent_first_and_leaves_the_standing_count_alone(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    control.authorize_pilot(db, task.key, SHA_A, founder=FOUNDER_ID)
    assert control.consume_write_authorization(db, task, SHA_A, 7) is None
    assert authorization(db).value["consumed_at"] and standing(db).value["used"]["count"] == 0


@pytest.mark.parametrize("label", ["other file", "no paths", "wrong sha", "expired", "revoked", "no row"])
def test_anything_outside_the_grant_is_denied_and_spends_nothing(monkeypatch, tmp_path, label):
    db, task, ps = armed(monkeypatch, tmp_path)
    issued = datetime.now(timezone.utc)
    if label == "expired":
        control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, days=1, now=issued - timedelta(days=3))
    elif label == "other file":
        control.issue_standing(db, ["tests/test_goals.py"], SHA_A, founder=FOUNDER_ID)
    elif label == "no paths":
        control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
        task.context = {**task.context, "stage_c": {}}
        db.commit()
    elif label == "wrong sha":
        control.issue_standing(db, [PATH], SHA_B, founder=FOUNDER_ID)
    elif label == "revoked":
        control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
        assert control.revoke_standing(db, actor=FOUNDER_ID)
    out, skipped = tick(db, ps)
    assert skipped and skipped <= {"stage_c_standing_scope", "stage_c_standing_invalid", "stage_c_standing_expired",
                                   "stage_c_authorization_missing", "stage_c_scope_refused"}, (label, skipped)
    assert_untouched(db, task, out)
    row = standing(db)
    assert row is None or row.value["used"]["count"] == 0



@pytest.mark.parametrize("value", [None, [], "yes", {}, {"paths": [PATH]},
                                   {"repository": "seerxo/seerxo-os", "source_sha": SHA_A, "issued_by": "founder",
                                    "action": "stage_c.code_write.standing", "paths": [PATH], "max_per_day": 99,
                                    "issued_at": "yesterday", "expires_at": "tomorrow"}])
def test_a_malformed_or_hand_written_row_is_denied(monkeypatch, tmp_path, value):
    db, task, ps = armed(monkeypatch, tmp_path)
    db.add(models.ControlState(key=STANDING, value=value, updated_by="founder")); db.commit()
    out, skipped = tick(db, ps)
    assert skipped == {"stage_c_standing_invalid"}
    assert_untouched(db, task, out)


def test_a_row_written_without_the_issue_audit_is_denied(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    value = control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    for a in db.scalars(select(models.AuditLog).where(models.AuditLog.action == "stage-c.standing.issue")):
        db.delete(a)
    db.commit()
    assert control.write_authorization_denial(db, task, SHA_A) == "stage_c_standing_invalid", value


def test_the_global_flag_and_host_acceptance_still_decide(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    monkeypatch.setattr(worker, "ENABLE_AUTONOMOUS_CODE_WRITE", False)
    out, skipped = tick(db, ps)
    assert skipped == {"code_write_disabled"} and standing(db).value["used"]["count"] == 0
    assert_untouched(db, task, out)


# ------------------------------------------------------------------------------------------- issuing it
@pytest.mark.parametrize("who", ["founder", "company-worker", "operator:local", "service:admin", "anonymous", "", None,
                                 "founder:access:", "founder:access:UPPER@x.io"])
def test_no_non_founder_string_can_issue_it(monkeypatch, tmp_path, who):
    db, task, ps = armed(monkeypatch, tmp_path)
    with pytest.raises(PermissionError):
        control.issue_standing(db, [PATH], SHA_A, founder=who)
    assert standing(db) is None and "stage-c.standing.issue" not in audit_actions(db)


@pytest.mark.parametrize("kwargs", [
    {"paths": ["app/main.py"]}, {"paths": ["app/api_auth.py"]}, {"paths": ["docker-compose.yml"]},
    {"paths": ["../escape.py"]}, {"paths": []}, {"paths": [PATH, PATH]}, {"paths": "tests/test_metrics.py"},
    {"days": 8}, {"days": 0}, {"max_per_day": 4}, {"max_per_day": 0}, {"max_per_day": True}, {"sha": "short"}])
def test_it_cannot_name_a_protected_file_or_exceed_the_bounds(monkeypatch, tmp_path, kwargs):
    db, task, ps = armed(monkeypatch, tmp_path)
    args = {"paths": [PATH], "sha": SHA_A, "days": 3, "max_per_day": 1, **kwargs}
    with pytest.raises(ValueError):
        control.issue_standing(db, args["paths"], args["sha"], founder=FOUNDER_ID, days=args["days"],
                               max_per_day=args["max_per_day"])
    assert standing(db) is None


def test_issuing_is_audited_and_revoking_removes_it(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    assert audit_actions(db)[-1] == "stage-c.standing.issue"
    for who in ("founder", "anonymous", None):
        with pytest.raises(PermissionError):
            control.revoke_standing(db, actor=who)
    assert standing(db) is not None
    assert control.revoke_standing(db, actor="operator:host-root") is True
    assert standing(db) is None and audit_actions(db)[-1] == "stage-c.standing.revoke"
    assert control.revoke_standing(db, actor=FOUNDER_ID) is False


def test_the_standing_grant_is_never_longer_than_a_week(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, days=7)
    row = standing(db)
    row.value = {**row.value, "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}; db.commit()
    assert control.write_authorization_denial(db, task, SHA_A) == "stage_c_standing_invalid"


# ------------------------------------------------------------------------------------------- the route
@pytest.mark.parametrize("label,headers,host", CALLERS)
def test_only_the_verified_founder_can_issue_it_over_http(env, label, headers, host):   # noqa: F811
    engine, _ = env
    r = _c(host).post("/stage-c/standing", json={"paths": [PATH], "days": 3, "max_per_day": 1}, headers=headers)
    assert r.status_code in (401, 403), label
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        assert standing(s) is None


def test_the_founder_issues_it_bound_to_the_deployed_sha_and_revokes_it(env):   # noqa: F811
    engine, _ = env
    r = _founder().post("/stage-c/standing", json={"paths": [PATH], "days": 2, "max_per_day": 2, "source_sha": "e" * 40},
                        headers=ASSERT)
    assert r.status_code == 201 and r.json()["source_sha"] == SHA and r.json()["issued_by"] == FID
    assert r.json()["paths"] == [PATH] and r.json()["max_per_day"] == 2
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        assert standing(s).updated_by == FID
    assert _founder().post("/stage-c/standing", json={"paths": ["app/main.py"]}, headers=ASSERT).status_code == 422
    assert _founder().post("/stage-c/standing", json={"paths": [PATH], "days": 9}, headers=ASSERT).status_code == 422
    assert _founder().post("/stage-c/standing/revoke", json={"reason": "done"}, headers=ASSERT).json() == {"revoked": True}


def test_standing_routes_are_enforce_always_and_founder_scoped():
    from app import api_auth
    for probe in ("/stage-c/standing", "/stage-c/standing/revoke"):
        assert api_auth._match(api_auth.ENFORCE_ALWAYS, "POST", probe) and api_auth._match(api_auth.FOUNDER_SCOPE, "POST", probe)


# ------------------------------------------------------------------------- provider capacity failures
def test_a_capacity_failure_gives_the_standing_run_back_exactly_once(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=1)
    now = datetime.now(timezone.utc)
    assert control.consume_write_authorization(db, task, SHA_A, 7, now) is None
    db.commit()
    # Break caught: a Codex quota outage burns the day's only run twice (seen on the host, TASK-0055 runs 33 and 34).
    assert control.refund_standing_run(db, task, 7, now) is True
    db.commit()
    assert standing(db).value["used"]["count"] == 0
    assert control.refund_standing_run(db, task, 7, now) is False          # not refundable twice
    assert control.refund_standing_run(db, task, 8, now) is False          # never consumed
    assert standing(db).value["used"]["count"] == 0
    assert control.consume_write_authorization(db, task, SHA_A, 9, now) is None      # the run is usable again
    db.commit()
    assert control.consume_write_authorization(db, task, SHA_A, 10, now) == "stage_c_standing_daily_limit"


def test_the_cap_still_holds_and_a_rolled_over_day_refunds_nothing(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=1)
    now = datetime.now(timezone.utc)
    assert control.consume_write_authorization(db, task, SHA_A, 1, now) is None
    db.commit()
    assert control.refund_standing_run(db, task, 1, now + timedelta(days=1)) is False
    assert standing(db).value["used"]["count"] == 1


def _spy_refunds(monkeypatch):
    refunds, real = [], control.refund_standing_run
    monkeypatch.setattr(control, "refund_standing_run", lambda *a, **k: refunds.append(a[1].key) or real(*a, **k))
    return refunds


def test_the_stage_c_worker_refunds_a_capacity_failure(monkeypatch, tmp_path):
    from tests.test_stage_c_worker import Reviewer, _db, pipeline, write_task
    from app import stage_c_runner
    pipeline(monkeypatch, tmp_path)
    refunds = _spy_refunds(monkeypatch)
    def quota(*a, **k):
        raise RuntimeError("subscription usage limit reached")
    monkeypatch.setattr(stage_c_runner, "launcher_coder", lambda **kw: quota)
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    assert out["verdict"] == "CODER_FAILED" and refunds == [task.key]      # Break caught: the call site is missing


def test_the_stage_c_worker_does_not_refund_a_completed_run(monkeypatch, tmp_path):
    from tests.test_stage_c_worker import Reviewer, _db, pipeline, write_task
    pipeline(monkeypatch, tmp_path)
    refunds = _spy_refunds(monkeypatch)
    db = _db()
    write_task(db)
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    assert out["verdict"] == "PR_OPEN" and refunds == []
