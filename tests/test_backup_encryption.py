"""scripts/backup.sh: secret-bearing targets leave the host only as .age when a recipient is set, and a dump
leaves the host only after the whole archive read back."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"
# The script runs on the Linux host (GNU stat/sha256sum); CI is Linux.
pytestmark = pytest.mark.skipif(subprocess.run(["stat", "-c", "%s", "/"], capture_output=True).returncode != 0
                                or not shutil.which("sha256sum"), reason="needs GNU coreutils")

# Stub docker: logs every call and report body; pg_dump prints a body; age writes its -o file.
DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$*" in
  *pg_dump*) echo dump ;;
  *"pg_restore --list"*) exit 0 ;;   # a table of contents is still there in a dump cut off near its end
  *"pg_restore --file=/dev/null"*) cat > /dev/null; exit "${RESTORE_RC:-0}" ;;   # the full read-back on stdin
  *curl*) cat >> "$CALLS"; echo >> "$CALLS"  # the report JSON arrives on stdin
          if [ "${REPORT_HTTP_STATUS:-}" = "500" ] && [[ "$*" == *"curl -fsS"* ]]; then exit 22; fi ;;
  *" age "*) out=""; prev=""; for a in "$@"; do [ "$prev" = -o ] && out=$a; prev=$a; done
             echo enc > "$BK/${out#/backups/}" ;;
esac
exit 0
"""


def _run(tmp_path, target, recipient, environment="production", code=0, restore_rc=0, report_http_status=""):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "docker").write_text(DOCKER)
    (tmp_path / "bin" / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "ENV_FILE": "/nonexistent",
           "BACKUP_LOCAL_DIR": str(tmp_path), "BK": str(tmp_path), "CALLS": str(tmp_path / "calls"),
           "BACKUP_AGE_RECIPIENT": recipient, "ENVIRONMENT": environment, "RESTORE_RC": str(restore_rc),
           "REPORT_HTTP_STATUS": report_http_status}
    out = subprocess.run(["bash", str(SCRIPT), target], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == code, out.stdout + out.stderr
    return (tmp_path / "calls").read_text().splitlines()


def _uploads(calls):
    return [c for c in calls if "scripts.r2 upload" in c]


def test_coolify_db_is_encrypted_before_it_leaves_the_host(tmp_path):
    calls = _run(tmp_path, "coolify-db", "age1examplerecipient")
    age = next(i for i, c in enumerate(calls) if " age -r age1examplerecipient " in c)
    # Break caught: the plaintext dump (every app's env vars) is what reaches R2.
    [up] = _uploads(calls)
    assert calls.index(up) > age and up.endswith(".dump.age")


def test_company_db_and_unset_recipient_stay_as_before(tmp_path):
    calls = _run(tmp_path, "company-db", "age1examplerecipient")
    assert not any(" age " in c for c in calls) and _uploads(calls)[0].endswith(".dump")


def test_production_without_a_recipient_fails_closed(tmp_path):
    calls = _run(tmp_path, "coolify-db", "", code=1)
    # Break caught: every app's env vars reach R2 in plaintext because a variable was missing.
    assert _uploads(calls) == []
    assert any("BACKUP_AGE_RECIPIENT unset" in c for c in calls)  # reported -> BACKUP_FAILED


def test_outside_production_secrets_upload_unencrypted_with_a_warning(tmp_path):
    calls = _run(tmp_path, "coolify-db", "", environment="development")
    assert _uploads(calls)[0].endswith(".dump")
    assert "uploading unencrypted" in (tmp_path / "backup.log").read_text()


def test_the_whole_dump_is_read_back_by_the_database_container_that_wrote_it(tmp_path):
    calls = _run(tmp_path, "company-db", "")
    # Not `pg_restore --list` (table of contents only) and not company-api's older client.
    [check] = [c for c in calls if "pg_restore" in c]
    assert check == "exec -i company-postgres pg_restore --file=/dev/null"
    assert calls.index(check) < calls.index(_uploads(calls)[0])


def test_a_dump_that_does_not_read_back_completely_never_leaves_the_host(tmp_path):
    calls = _run(tmp_path, "company-db", "", code=1, restore_rc=1)
    # Break caught: a dump cut off near its end passed `--list`, was uploaded and reported as verified.
    assert _uploads(calls) == []
    assert any('"integrity_verified": false' in c and "local integrity check failed" in c for c in calls)


def test_api_rejecting_backup_report_fails_the_backup_job(tmp_path):
    calls = _run(tmp_path, "company-db", "", code=1, report_http_status="500")
    assert any("curl -fsS" in c for c in calls)
    assert "WARN could not report 'company-db' to Company OS" in (tmp_path / "backup.log").read_text()
    assert "backup end failed=1" in (tmp_path / "backup.log").read_text()


def test_local_prune_uses_each_backup_class_and_requires_verified_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_RETENTION_DAILY", "1")
    monkeypatch.setenv("BACKUP_RETENTION_WEEKLY", "20")
    monkeypatch.setenv("BACKUP_RETENTION_MONTHLY", "20")
    names = (
        "2026-10-05-company-db-2026-10-05T000000Z.dump",  # Monday: daily
        "2026-10-06-company-db-2026-10-06T000000Z.dump",  # Tuesday: daily
        "2026-10-04-company-db-2026-10-04T000000Z.dump",  # Sunday: weekly
        "2026-10-01-company-db-2026-10-01T000000Z.dump",  # first: monthly
        "2026-10-04-renders-2026-10-04T000000Z.tar.gz",
        "2026-09-27-renders-2026-09-27T000000Z.tar.gz",
        "manual-export.tar.gz",  # unknown local files must not be pruned
    )
    for name in names:
        path = tmp_path / name
        path.write_text("archive")

    _run(tmp_path, "company-db", "")

    assert not (tmp_path / names[0]).exists()
    assert all((tmp_path / name).exists() for name in names[2:])


def test_failed_backup_keeps_older_local_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_RETENTION_DAILY", "1")
    old = tmp_path / "2026-10-05-company-db-2026-10-05T000000Z.dump"
    old.write_text("previous backup")
    newer = tmp_path / "2026-10-06-company-db-2026-10-06T000000Z.dump"
    newer.write_text("another backup")

    _run(tmp_path, "company-db", "", code=1, restore_rc=1)

    assert old.exists() and newer.exists()
