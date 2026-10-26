"""Founder attention queue: deterministic, ordered by founder authority, no fake alarms."""
from datetime import datetime, timedelta, timezone

from app import enums, models
from app.desk import needs_you, provider_health

NOW = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)
HEALTHY = {"status": "HEALTHY", "heartbeat_at": NOW.isoformat(), "heartbeat_age_s": 5,
           "providers": {"claude": "AVAILABLE", "codex": "AVAILABLE"}, "cooldown": {}}


def _backup(hours_ago=2, ok=True, verified=True):
    return models.BackupRun(target="company-db", ok=ok, uploaded=verified, integrity_verified=verified,
                            started_at=NOW - timedelta(hours=hours_ago))


def _q(approvals=(), incidents=(), tasks=(), worker=None, backup="fresh", paused=False,
       backup_verified=None):
    row = _backup() if backup == "fresh" else backup
    return needs_you(list(approvals), list(incidents), list(tasks), worker or HEALTHY,
                     row, paused, now=NOW,
                     backup_verified=(backup == "fresh") if backup_verified is None else backup_verified)


def _incident(sev, key="INC-0001"):
    return models.Incident(key=key, severity=sev, title=f"{sev.value} problem", description="d",
                           started_at=NOW - timedelta(hours=3), detected_by="watchdog",
                           status=enums.IncidentStatus.INVESTIGATING)


def _blocked(priority, key):
    return models.Task(key=key, title=f"blocked {priority.value}", success_criteria="s",
                       status=enums.TaskStatus.BLOCKED, priority=priority,
                       risk_level=enums.RiskLevel.LOW, updated_at=NOW - timedelta(hours=1))


def test_healthy_unattended_company_needs_nothing():
    # Break caught: a calm company shown as needing attention trains the founder to ignore it.
    assert _q() == []


def test_items_follow_founder_authority_order():
    approval = models.Approval(key="APR-0001", action="Deploy", reason="fix", risk_level=enums.RiskLevel.MEDIUM,
                               requested_by="developer", created_at=NOW - timedelta(minutes=5))
    out = _q(approvals=[approval],
             incidents=[_incident(enums.IncidentSeverity.SEV1), _incident(enums.IncidentSeverity.SEV2, "INC-2"),
                        _incident(enums.IncidentSeverity.SEV0, "INC-0")],
             tasks=[_blocked(enums.Priority.P1, "TASK-1"), _blocked(enums.Priority.P0, "TASK-0"),
                    _blocked(enums.Priority.P2, "TASK-2")],
             paused=True)
    assert [i["kind"] for i in out] == ["SEV0", "SEV1", "APPROVAL", "BLOCKED_P0", "BLOCKED_P1", "OTHER"]
    assert out[2]["target"] == {"page": "approvals", "key": "APR-0001"}
    assert all(i["title"] and i["why"] and i["action"] for i in out)


def test_one_provider_at_quota_is_capacity_not_a_founder_problem():
    one = {**HEALTHY, "providers": {"claude": "QUOTA_EXHAUSTED", "codex": "AVAILABLE"}}
    assert _q(worker=one) == []
    both = {**HEALTHY, "providers": {"claude": "QUOTA_EXHAUSTED", "codex": "RATE_LIMITED"}}
    [item] = _q(worker=both)
    # Break caught: quota shown as a failure (or as something to buy).
    assert item["level"] == "info" and "no retry is consumed" in item["why"]
    assert "buy" not in item["action"].lower()


def test_login_problem_needs_the_founder():
    w = {**HEALTHY, "providers": {"claude": "AUTH_REQUIRED", "codex": "AVAILABLE"}}
    [item] = _q(worker=w)
    assert item["kind"] == "PROVIDER" and item["level"] == "warning"


def test_active_cooldown_overrides_last_local_check():
    w = {**HEALTHY, "cooldown": {"claude": {"until": (NOW + timedelta(hours=1)).isoformat(),
                                            "health": "QUOTA_EXHAUSTED"}}}
    assert provider_health(w, NOW)["claude"] == "QUOTA_EXHAUSTED"
    expired = {**HEALTHY, "cooldown": {"claude": {"until": (NOW - timedelta(minutes=1)).isoformat(),
                                                  "health": "QUOTA_EXHAUSTED"}}}
    assert provider_health(expired, NOW)["claude"] == "AVAILABLE"


def test_silent_worker_and_missing_backup_are_critical():
    stale = {**HEALTHY, "status": "STALE", "heartbeat_age_s": 1800}
    kinds = [(i["kind"], i["level"], i["title"]) for i in _q(worker=stale, backup=None)]
    assert ("STOPPED", "critical", "Autonomous worker stopped reporting") in kinds
    assert ("STOPPED", "critical", "No verified off-site database backup") in kinds


def test_upload_alone_is_not_a_verified_backup():
    item = _q(backup=_backup(ok=True, verified=False))[0]
    assert item["title"] == "Latest database backup is not verified off-site"
    old_backup = _backup(hours_ago=48)
    [old] = _q(backup=old_backup, backup_verified=True)
    assert old["level"] == "warning" and "too old" in old["title"]


def test_successful_backup_without_its_verification_event_needs_attention():
    # A good run row is not enough: only its matching durable event proves verification.
    item = _q(backup=_backup(ok=True, verified=True), backup_verified=False)[0]
    assert item["title"] == "Latest database backup is not verified off-site"
    assert "verified_event=False" in item["evidence"]
