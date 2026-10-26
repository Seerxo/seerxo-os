"""backup.sh prune_local: class counts and verified replacement before local deletion."""
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup.sh"


def _run(tmp_path, files, uploaded=()):
    source = SCRIPT.read_text()
    body = "\n".join(re.search(rf"^{fn}\(\) \{{.*?^\}}", source, re.S | re.M).group(0)
                     for fn in ("prune_local_target", "prune_local"))
    for name, age in files.items():
        f = tmp_path / name
        f.write_bytes(b"x")
        t = time.time() - age * 86400
        os.utime(f, (t, t))
    script = (f'exec_log() {{ :; }}\nBACKUP_LOCAL_DIR={shlex.quote(str(tmp_path))}\n'
              f'LOG=/dev/null\nUPLOADED=({" ".join(uploaded)})\n{body}\nprune_local')
    subprocess.run(["bash", "-c", script], check=True,
                   env={"PATH": "/usr/bin:/bin", "RETENTION_DAILY": "1", "RETENTION_WEEKLY": "1",
                        "RETENTION_MONTHLY": "1"})
    return sorted(p.name for p in tmp_path.iterdir())


@pytest.mark.skipif(sys.platform == "darwin" and not os.environ.get("CIRCLECI"),
                    reason="script uses GNU date -d; the CircleCI Linux job always runs this")
def test_class_counts_and_renders_require_verified_replacement(tmp_path):
    files = {"2026-10-05-company-db-2026-10-05T000000Z.dump": 10,
             "2026-10-06-company-db-2026-10-06T000000Z.dump": 10,
             "2026-10-06-company-db-2026-10-06T000000Z.dump.age": 10,
             "2026-09-27-company-db-2026-09-27T000000Z.dump": 10,
             "2026-10-04-company-db-2026-10-04T000000Z.dump": 10,
             "2026-09-01-company-db-2026-09-01T000000Z.dump": 10,
             "2026-10-01-company-db-2026-10-01T000000Z.dump": 10,
             "2026-10-04-renders-2026-10-04T000000Z.tar.gz": 2,
             "2026-10-11-renders-2026-10-11T000000Z.tar.gz": 1,
             "notes.txt": 99}
    assert _run(tmp_path, files) == sorted(files)
    assert _run(tmp_path, files, uploaded=("company-db", "renders")) == sorted(set(files) - {
        "2026-10-05-company-db-2026-10-05T000000Z.dump",
        "2026-09-27-company-db-2026-09-27T000000Z.dump",
        "2026-09-01-company-db-2026-09-01T000000Z.dump",
        "2026-10-04-renders-2026-10-04T000000Z.tar.gz",
    })


def test_failed_r2_round_trip_does_not_add_renders_to_the_prune_allowlist(tmp_path):
    source = SCRIPT.read_text()
    functions = "\n".join(re.search(rf"^{fn}\(\) \{{.*?^\}}", source, re.S | re.M).group(0)
                           for fn in ("finish_target", "prune_local_target", "prune_local"))
    old = tmp_path / "2026-10-04-renders-2026-10-04T000000Z.tar.gz"
    new = tmp_path / "2026-10-11-renders-2026-10-11T013003Z.tar.gz"
    old.write_bytes(b"old verified archive")
    new.write_bytes(b"new archive with failed remote verification")
    script = ("exec_log() { :; }\nreport() { :; }\n"
              "r2_configured() { return 0; }\n"
              "r2() { [ \"$1\" = upload ] && return 0; [ \"$1\" = verify ] && return 1; return 2; }\n"
              "stat() { wc -c < \"$3\" | awk '{print $1}'; }\n"
              "sha256sum() { shasum -a 256 \"$1\"; }\n"
              f"BACKUP_LOCAL_DIR={shlex.quote(str(tmp_path))}\n"
              "LOG=/dev/null\nCLASS=weekly\nENCRYPT_TARGETS=' '\n"
              "LOCAL_KEEP_RENDERS=1\nRETENTION_DAILY=1\nRETENTION_WEEKLY=1\nRETENTION_MONTHLY=1\n"
              "FAILED=0\nUPLOADED=()\n" + functions + "\n"
              "finish_target renders \"$BACKUP_LOCAL_DIR/2026-10-11-renders-2026-10-11T013003Z.tar.gz\" 1 || :\n"
              "prune_local\nprintf '%s\\n' \"$FAILED:${#UPLOADED[@]}\"")
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0
    assert result.stdout.strip() == "1:0"
    assert old.exists() and new.exists()
