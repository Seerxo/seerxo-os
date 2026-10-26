"""Verified reports can release a wait; incomplete backups cannot."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import enums, main, models, notify, schemas, watchdog, worker
from tests.test_worker import SPECS, _db, _task


def report(**changes):
    return schemas.BackupReport(**{
        "target": "company-db", "destination": "company-db/example.dump",
        "size_bytes": 128, "checksum": "a" * 64, "uploaded": True,
        "integrity_verified": True, "ok": True, **changes,
    })


def test_verified_backup_event_releases_only_the_matching_wait(monkeypatch):
    monkeypatch.setattr(worker, "ENABLE_AUTOMATIC_EXTERNAL_RELEASE", True)
    with _db() as db:
        task = _task(db, status=enums.TaskStatus.WAITING_EXTERNAL)
        task.context = {**task.context, "external_wait": {
            "source": "company-backup", "event_type": "BACKUP_VERIFIED",
            "match": {"target": "company-db", "status": "VERIFIED"},
            "after_event_id": 0,
            "since": (datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat(),
        }}
        db.commit()
        result = main.report_backup(report(target="volumes"), db)
        assert result["notified"] is False
        assert worker.release_external_waits(db, SPECS) == []
        result = main.report_backup(report(), db)
        event = db.scalars(select(models.Event).where(
            models.Event.event_type == "BACKUP_VERIFIED",
            models.Event.payload["target"].as_string() == "company-db")).one()
        assert event.payload["backup_run_id"] == result["id"]
        assert event.payload["evidence"] == [f"backup_run:{result['id']}", "sha256:" + "a" * 64]
        assert event.dedupe_key == f"backup-verified:{result['id']}"
        assert worker.release_external_waits(db, SPECS) == [task.key]
        assert worker.release_external_waits(db, SPECS) == []
        assert db.get(models.Task, task.id).status == enums.TaskStatus.READY


@pytest.mark.parametrize("changes", [
    {"uploaded": False}, {"integrity_verified": False}, {"expected": True},
    {"checksum": None}, {"checksum": "not-a-sha256"}, {"destination": None},
    {"size_bytes": 0}, {"error": "verification failed"},
])
def test_incomplete_or_contradictory_report_never_emits_success(changes):
    with _db() as db:
        main.report_backup(report(**changes), db)
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.event_type == "BACKUP_VERIFIED")) == 0


def test_backup_run_and_success_event_rollback_together(monkeypatch):
    with _db() as db:
        def fail_commit():
            raise RuntimeError("commit unavailable")
        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit unavailable"):
            main.report_backup(report(), db)
        db.rollback()
        assert db.scalar(select(func.count()).select_from(models.BackupRun)) == 0
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.event_type == "BACKUP_VERIFIED")) == 0


def test_new_failure_after_verified_recovery_pages_again(monkeypatch):
    class Adapter:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    adapter = Adapter()
    monkeypatch.setattr(main, "notifier", notify.NotificationService(adapter))
    with _db() as db:
        first = main.report_backup(report(ok=False, error="first failure"), db)
        repeated = main.report_backup(report(ok=False, error="same failure"), db)
        assert first["notified"] is True and repeated["notified"] is False
        main.report_backup(report(), db)
        again = main.report_backup(report(ok=False, error="new failure"), db)
        assert again["notified"] is True
        assert len(adapter.sent) == 3  # failure, verified recovery, new failure


def test_watchdog_recovery_requires_verified_backup_event():
    with _db() as db:
        main.report_backup(report(ok=True, uploaded=False), db)
        main.report_backup(report(ok=False, restore_tested=True, error="restore failed"), db)
        assert watchdog._last_verified_backup(db, restore=False) is None
        assert watchdog._last_verified_backup(db, restore=True) is None
        backup = main.report_backup(report(), db)
        restore = main.report_backup(report(restore_tested=True), db)
        assert watchdog._last_verified_backup(db, restore=False).id == backup["id"]
        assert watchdog._last_verified_backup(db, restore=True).id == restore["id"]
