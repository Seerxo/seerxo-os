"""Company state must not lose prior successful restore evidence after a new backup."""
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import main, models


def test_brief_reports_latest_successful_restore_separately_from_latest_backup():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    restore_at = datetime(2026, 10, 11, 2, 30, tzinfo=timezone.utc)
    failed_restore_at = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)
    backup_at = datetime(2026, 10, 13, 1, 30, tzinfo=timezone.utc)

    with Session(engine) as db:
        db.add_all([
            models.BackupRun(
                target="company-db", started_at=restore_at, ended_at=restore_at,
                ok=True, integrity_verified=True, uploaded=True, restore_tested_at=restore_at,
            ),
            models.BackupRun(
                target="company-db", started_at=failed_restore_at, ended_at=failed_restore_at,
                ok=False, integrity_verified=False, restore_tested_at=failed_restore_at,
            ),
            models.BackupRun(
                target="company-db", started_at=backup_at, ended_at=backup_at,
                ok=True, integrity_verified=True,
            ),
        ])
        db.commit()

        state = main._state(db)
        brief = main.company_brief(db)

    assert state["last_backup"]["at"].startswith("2026-10-13T01:30:00")
    assert state["last_backup"]["restore_tested_at"] is None
    assert state["last_restore"]["target"] == "company-db"
    assert state["last_restore"]["at"].startswith("2026-10-11T02:30:00")
    assert f"last successful restore test: {state['last_restore']['at']}" in brief
    assert "last successful restore test: NEVER" not in brief


def test_a_local_dump_restore_is_not_reported_as_off_site_recovery():
    # A restore from the local dump (uploaded=False) proves the file, not off-site recovery.
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    local_at = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)

    with Session(engine) as db:
        db.add(models.BackupRun(
            target="company-db", started_at=local_at, ended_at=local_at,
            ok=True, integrity_verified=True, uploaded=False, restore_tested_at=local_at,
        ))
        db.commit()
        state = main._state(db)

    assert state["last_restore"] is None
