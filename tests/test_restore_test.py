"""scripts/restore-test.sh: a restore that pg_restore could not complete is a failed restore, never a pass.

The stub tests use a fake `docker`. The last test runs the real pg_dump/pg_restore/psql against the disposable
PostgreSQL (EXTERNAL_RELEASE_TEST_DATABASE_URL) when the local client is at least the server's version.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "restore-test.sh"

# Stub docker: R2 unconfigured; pg_restore exits RESTORE_RC; the verification query returns COUNTS; reports logged.
STUB = r"""#!/usr/bin/env bash
case "$*" in
  *"scripts.r2"*) exit 10 ;;
  *curl*)
    cat >> "$REPORTS"; echo >> "$REPORTS"
    if [ "${CURL_HTTP_STATUS:-201}" -ge 400 ] && [[ " $* " == *" -f "* ]]; then exit 22; fi
    ;;
  *pg_restore*) cat > /dev/null; exit "$RESTORE_RC" ;;
  *"information_schema.tables"*) echo "$COUNTS"; exit "${QUERY_RC:-0}" ;;
esac
exit 0
"""

R2_STUB = r"""#!/usr/bin/env bash
case "$*" in
  *"scripts.r2 check"*) exit 0 ;;
  *"scripts.r2 list company-db/"*) printf '%s\n' "$R2_KEY_LINE" ;;
  *"/app/scripts/r2.py download"*)
    printf '%s\0' "$@" > "$DOCKER_ARGS_FILE"
    if [ "${R2_DOWNLOAD_FAIL:-0}" = 1 ]; then
      printf 'download failed key=%s\n' "$R2_KEY_VALUE" >&2
      exit 1
    fi
    printf 'downloaded %s\n' "$R2_KEY_VALUE"
    printf 'PGDMP' > "$BACKUP_LOCAL_DIR/.restore-test.dump"
    ;;
  *"/backups/.restore-test.dump"*) printf 'PGDMP' > "$BACKUP_LOCAL_DIR/.restore-test.dump" ;;
  *curl*) cat >> "$REPORTS"; echo >> "$REPORTS" ;;
  *pg_restore*) cat > /dev/null; exit 0 ;;
  *"information_schema.tables"*) echo "$COUNTS" ;;
esac
exit 0
"""

# Real tools: `docker exec [-i] [-e NAME=V]... CONTAINER CMD...` runs CMD here, against PGHOST/PGPORT.
REAL = r"""#!/usr/bin/env bash
[ "$1" = exec ] || exit 1; shift
while [[ "${1:-}" == -* ]]; do case $1 in -i) shift ;; -e) case $2 in *=*) export "$2" ;; esac; shift 2 ;; *) shift ;; esac; done
shift
case $1 in python) exit 10 ;; curl) cat >> "$REPORTS"; echo >> "$REPORTS"; exit 0 ;; esac
exec "$@"
"""


def _run(tmp_path, dump: bytes, docker: str, **env):
    bin_dir, backups = tmp_path / "bin", tmp_path / "backups"
    bin_dir.mkdir()
    backups.mkdir()
    (bin_dir / "docker").write_text(docker)
    (bin_dir / "docker").chmod(0o755)
    (backups / "2026-10-09-company-db-2026-10-09T013001Z.dump").write_bytes(dump)
    reports = tmp_path / "reports"
    reports.touch()
    full = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ENV_FILE": "/nonexistent",
            "BACKUP_LOCAL_DIR": str(backups), "REPORTS": str(reports),
            "R2_KEY_LINE": "2026-10-14T01:30:00+00:00\t5\tcompany-db/daily/example.dump",
            "R2_KEY_VALUE": "company-db/daily/example.dump",
            "DOCKER_ARGS_FILE": str(tmp_path / "docker-args"), **env}
    out = subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=120)
    return out, [json.loads(line) for line in reports.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("counts", ["22 8 11 5", "10 1 0 0"])
def test_a_complete_restore_with_the_company_in_it_passes(tmp_path, counts):
    out, [report] = _run(tmp_path, b"PGDMP", STUB, RESTORE_RC="0", COUNTS=counts)
    assert out.returncode == 0, out.stdout + out.stderr
    assert report["ok"] is True and report["restore_tested"] is True
    assert report["uploaded"] is False
    assert report["destination"].startswith("local:")
    assert report["size_bytes"] == len(b"PGDMP")
    assert report["checksum"] == hashlib.sha256(b"PGDMP").hexdigest()
    assert "source=local" in out.stdout


@pytest.mark.parametrize("counts", [
    "", "22 8 11", "22 8 11 5 9", "not-a-number not-a-number 11 5",
    "-22 8 11 5", "22 -8 11 5", "22 8 -11 5", "22 8 11 -5", "22 8 11 invalid",
    "22 8\n11 5", "9 8 11 5", "22 0 11 5",
])
def test_restore_counts_must_prove_the_complete_nonnegative_company_sample(tmp_path, counts):
    out, [report] = _run(tmp_path, b"PGDMP", STUB, RESTORE_RC="0", COUNTS=counts)
    assert out.returncode == 1
    assert report["ok"] is False and report["integrity_verified"] is False
    assert "restore verification" in report["error"]
    assert "=== restore test PASSED ===" not in out.stdout


def test_failed_restore_verification_query_cannot_report_successful_counts(tmp_path):
    out, [report] = _run(tmp_path, b"PGDMP", STUB, RESTORE_RC="0", QUERY_RC="1", COUNTS="22 8 11 5")
    assert out.returncode == 1
    assert report["ok"] is False and report["integrity_verified"] is False
    assert "restore verification query failed" in report["error"]
    assert "=== restore test PASSED ===" not in out.stdout


def test_successful_r2_restore_reports_source_provenance(tmp_path):
    out, [report] = _run(tmp_path, b"PGDMP", R2_STUB, COUNTS="22 8 11 5", R2_BUCKET="company-backups")
    assert out.returncode == 0, out.stdout + out.stderr
    assert report["ok"] is True and report["restore_tested"] is True
    assert report["uploaded"] is True
    assert report["destination"] == "r2:company-db/daily/example.dump"
    assert report["size_bytes"] == len(b"PGDMP")
    assert report["checksum"] == hashlib.sha256(b"PGDMP").hexdigest()
    assert "source=r2" in out.stdout


def test_r2_object_key_is_passed_as_data_not_interpolated_as_code(tmp_path):
    key = "company-db/weekly/backup.dump'); __import__('os').system('touch /tmp/injected') #"
    listing = f"2026-10-14T01:30:00+00:00\t5\t{key}"
    out, [report] = _run(tmp_path, b"PGDMP", R2_STUB, COUNTS="22 8 11 5", R2_BUCKET="company-backups",
                          R2_KEY_LINE=listing, R2_KEY_VALUE=key)

    assert out.returncode == 0, out.stdout + out.stderr
    assert report["destination"] == f"r2:{key}"
    args = (tmp_path / "docker-args").read_bytes().split(b"\0")
    download = args.index(b"/app/scripts/r2.py")
    assert args[download - 1] == b"python"
    assert args[download + 1:download + 4] == [b"download", key.encode(), b"/backups/.restore-test.dump"]
    assert "sh -c" not in out.stdout
    assert key not in out.stdout
    assert key not in (tmp_path / "backups" / "restore-test.log").read_text()


def test_r2_download_error_cannot_write_object_key_to_restore_log(tmp_path):
    key = "company-db/weekly/error-key.dump"
    listing = f"2026-10-14T01:30:00+00:00\t5\t{key}"
    out, [report] = _run(tmp_path, b"PGDMP", R2_STUB, COUNTS="22 8 11 5", R2_BUCKET="company-backups",
                          R2_KEY_LINE=listing, R2_KEY_VALUE=key, R2_DOWNLOAD_FAIL="1")

    assert out.returncode == 0, out.stdout + out.stderr
    assert report["destination"].startswith("local:")
    assert report["uploaded"] is False
    assert key not in out.stdout
    assert key not in (tmp_path / "backups" / "restore-test.log").read_text()
    assert "R2 download failed, falling back to local dump" in out.stdout


def test_restore_job_fails_if_the_success_report_receives_an_http_error(tmp_path):
    out, [report] = _run(tmp_path, b"PGDMP", STUB, RESTORE_RC="0", COUNTS="22 8 11 5",
                          CURL_HTTP_STATUS="500")
    assert out.returncode == 1
    assert report["ok"] is True and report["restore_tested"] is True
    assert "could not report to Company OS" in out.stdout
    assert "=== restore test PASSED ===" not in out.stdout


def test_pg_restore_errors_fail_even_when_the_sampled_tables_look_fine(tmp_path):
    out, [report] = _run(tmp_path, b"PGDMP", STUB, RESTORE_RC="1", COUNTS="22 8 11 5")
    # Break caught: a dump cut off near its end restores every table and the first rows, so the sampled
    # counts pass while later tables are empty; the old script logged a WARN and reported success.
    assert out.returncode == 1
    assert report["ok"] is False and "pg_restore reported errors" in report["error"]


def _client_major(tool):
    path = shutil.which(tool)
    m = re.search(r"(\d+)", subprocess.run([path, "--version"], capture_output=True, text=True).stdout) if path else None
    return int(m.group(1)) if m else None


def test_real_postgres_a_truncated_dump_fails_and_a_complete_one_passes(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("disposable PostgreSQL URL not supplied")
    u = make_url(url)
    if u.database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    with engine.connect() as c:
        server = int(c.execute(text("show server_version_num")).scalar()) // 10000
    clients = [_client_major(t) for t in ("pg_dump", "pg_restore", "psql")]
    if None in clients or min(clients) < server:
        pytest.skip(f"local PostgreSQL client {clients} older than server {server}: pg_dump would refuse")
    from app import enums, models
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    try:
        with engine.begin() as c:
            for i in range(3):
                c.execute(models.BusinessUnit.__table__.insert().values(
                    key=f"unit-{i}", name="U", status=enums.BusinessUnitStatus.ACTIVE, priority=enums.Priority.P1))
            # Incompressible events, whose data comes after business_units in the archive: a 95% cut then keeps
            # the table of contents and every business unit, and loses only event rows.
            for i in range(400):
                c.execute(models.Event.__table__.insert().values(
                    event_type="TEST", source="restore-test", severity=list(enums.Severity)[0],
                    payload={"blob": os.urandom(96).hex()}))
        pg = {"PGHOST": u.host, "PGPORT": str(u.port or 5432), "PGUSER": u.username, "PGPASSWORD": u.password or "",
              "POSTGRES_USER": u.username, "POSTGRES_PASSWORD": u.password or ""}
        dump = subprocess.run(["pg_dump", "-Fc", "-d", u.database], env={**os.environ, **pg},
                              capture_output=True, check=True).stdout
        cut = dump[: len(dump) * 95 // 100]
        for name in ("good", "cut"):
            (tmp_path / name).mkdir()
        out, [report] = _run(tmp_path / "good", dump, REAL, **pg)
        assert out.returncode == 0 and report["ok"] is True, out.stdout + out.stderr
        assert _lists(cut, pg)   # main's integrity check (`pg_restore --list`) accepted this cut dump
        assert _reads_back(dump, pg) and not _reads_back(cut, pg)   # what backup.sh's integrity check now sees
        out, [report] = _run(tmp_path / "cut", cut, REAL, **pg)
        assert out.returncode == 1 and report["ok"] is False, out.stdout + out.stderr
    finally:
        models.Base.metadata.drop_all(engine)
        engine.dispose()


def _lists(dump: bytes, pg: dict) -> bool:
    return subprocess.run(["pg_restore", "--list"], input=dump, env={**os.environ, **pg},
                          capture_output=True).returncode == 0


def _reads_back(dump: bytes, pg: dict) -> bool:
    return subprocess.run(["pg_restore", "--file=/dev/null"], input=dump, env={**os.environ, **pg},
                          capture_output=True).returncode == 0
