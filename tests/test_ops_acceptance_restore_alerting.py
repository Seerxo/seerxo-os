"""V1 acceptance (docs/runbooks/V1_ACCEPTANCE.md, row "Restore never verified"): the watchdog must alert when no
verified off-site restore has EVER been recorded.

Before this fix `check_backups` raised `backup-never` for backups but checked restores only `if last_restore is
not None`, so a host whose weekly restore never ran, or only ever passed from the local fallback dump
(`uploaded=False`, which `_last_verified_backup` refuses as off-site proof), stayed silent forever.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import enums, models, watchdog


def _db() -> Session:
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return Session(engine)


def _verified(db: Session, at: datetime, *, restore: bool, n: int) -> None:
    run = models.BackupRun(target="company-db", started_at=at, ended_at=at, ok=True, uploaded=True,
                           integrity_verified=True, restore_tested_at=at if restore else None,
                           destination=f"r2:company-db/daily/{n}.dump", size_bytes=1, checksum=f"{n:064x}")
    db.add(run)
    db.flush()
    db.add(models.Event(event_type="BACKUP_VERIFIED", source="company-backup", severity=enums.Severity.INFO,
                        dedupe_key=f"backup-verified:{run.id}",
                        payload={"target": "company-db", "status": "VERIFIED", "backup_run_id": run.id}))


def _local_only_restore(db: Session, at: datetime) -> None:
    # restore-test.sh after an R2 failure: PASSED from the local dump, reported uploaded=False, never verified.
    db.add(models.BackupRun(target="company-db", started_at=at, ended_at=at, ok=True, uploaded=False,
                            integrity_verified=True, restore_tested_at=at,
                            destination="local:/opt/company-os/backups/x.dump", size_bytes=1, checksum="b" * 64))


def _restore_findings(db: Session) -> list:
    return [f for f in watchdog.check_backups(db) if f.event_type == "RESTORE_VERIFICATION_FAILED"]


@pytest.mark.parametrize("history", ["no_restore_ever", "only_local_fallback_restores"])
def test_no_verified_off_site_restore_ever_raises_a_high_restore_finding(history):
    now = datetime.now(timezone.utc)
    with _db() as db:
        _verified(db, now - timedelta(hours=2), restore=False, n=1)   # backups are fine: only the restore is missing
        if history == "only_local_fallback_restores":
            for weeks in (1, 2, 3):
                _local_only_restore(db, now - timedelta(weeks=weeks))
        db.commit()

        assert not [f for f in watchdog.check_backups(db) if f.event_type == "BACKUP_FAILED"]
        found = _restore_findings(db)

    assert [(f.dedupe, f.severity) for f in found] == [("restore-never", enums.Severity.HIGH)]


def test_a_fresh_verified_r2_restore_raises_nothing_and_a_stale_one_keeps_the_existing_alert():
    now = datetime.now(timezone.utc)
    with _db() as db:
        _verified(db, now - timedelta(hours=2), restore=False, n=1)
        _verified(db, now - timedelta(days=watchdog.RESTORE_TEST_MAX_AGE_D + 1), restore=True, n=2)
        db.commit()
        assert [f.dedupe for f in _restore_findings(db)] == ["restore-stale"]

        _verified(db, now - timedelta(hours=1), restore=True, n=3)
        db.commit()
        assert _restore_findings(db) == []


def test_restore_never_pages_once_across_ticks_survives_local_restores_and_closes_on_fresh_r2(monkeypatch):
    sent = []

    def dispatch(session, event):
        sent.append(event.event_type)
        return False, "test: no transport"

    monkeypatch.setattr(watchdog.notifier, "dispatch", dispatch)
    now = datetime.now(timezone.utc)
    tick = now - timedelta(hours=3)

    def run_tick(db):
        nonlocal tick
        tick += timedelta(minutes=10)                      # the cron cadence
        return watchdog.emit(db, watchdog.check_backups(db), {"collected_at": tick.isoformat()})

    with _db() as db:
        _verified(db, now - timedelta(hours=4), restore=False, n=1)
        db.commit()

        results = [run_tick(db) for _ in range(6)]           # one hour of ticks
        assert [r["emitted"] for r in results] == [1, 0, 0, 0, 0, 0]
        assert sent == ["RESTORE_VERIFICATION_FAILED"]

        _local_only_restore(db, tick)                        # a local fallback PASS proves nothing off-site
        db.commit()
        assert run_tick(db)["recovered"] == 0
        assert sent == ["RESTORE_VERIFICATION_FAILED"]

        _verified(db, tick, restore=True, n=2)               # fresh, verified, R2-sourced
        db.commit()
        closed = run_tick(db)
        assert closed["recovered"] == 1 and closed["emitted"] == 0
        assert db.scalar(select(func.count()).select_from(models.Event).where(
            models.Event.event_type == "RESTORE_VERIFICATION_FAILED")) == 1
