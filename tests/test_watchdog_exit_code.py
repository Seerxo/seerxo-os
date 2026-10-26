"""scripts/watchdog.sh, run as shipped, must tell "findings recorded" from "watchdog broken"."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "watchdog.sh"
STUBS = {
    # `docker exec -i company-api python -m app.watchdog` exits with $FAKE_RC; other calls are empty.
    "docker": 'case "$*" in *app.watchdog*) cat >/dev/null; exit "$FAKE_RC";; esac; exit 0',
    "curl": "echo 200",
    "company-notify": 'echo "$*" >> "$NOTIFY_LOG"',
}


def _run(tmp_path, app_rc):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "notify.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_RC": str(app_rc),
           "NOTIFY_LOG": str(log), "ENV_FILE": str(tmp_path / "absent.env")}
    out = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return out.returncode, log.read_text() if log.exists() else ""


@pytest.mark.parametrize("app_rc", [0, 1])
def test_a_normal_run_is_silent(tmp_path, app_rc):
    # Break caught: rc taken from `! cmd` is always 0, so recorded findings (exit 1) paged the
    # founder with "Watchdog could not run ... exited 0".
    assert _run(tmp_path, app_rc) == (0, "")


@pytest.mark.parametrize("app_rc", [2, 126])
def test_a_broken_watchdog_alerts_and_fails(tmp_path, app_rc):
    code, notified = _run(tmp_path, app_rc)
    assert code == app_rc and f"exited {app_rc}" in notified
