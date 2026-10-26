"""The watchdog's publish-age collector, run exactly as shipped in scripts/watchdog.sh."""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "watchdog.sh"


def _age(state_dir: Path) -> str:
    if shutil.which("node") is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("node is required in CI")
        pytest.skip("node not installed")
    js = re.search(r"^YT_AGE_JS='\n([^']*)'", SCRIPT.read_text(), re.M).group(1)  # JS uses no ' 
    out = subprocess.run(["node", "-e", js], env={**os.environ, "YT_STATE": str(state_dir)},
                         capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_publish_age_comes_from_the_upload_queue_and_ignores_shorts(tmp_path):
    (tmp_path / "upload-kuyruk.json").write_text(json.dumps({"isler": [
        {"is": "2026-10-04-flight-508", "durum": "yayinda", "yayinlandi": _iso(3)},
        {"is": "2026-10-04-flight-508-short", "durum": "yayinda", "yayinlandi": _iso(1)},
        {"is": "2026-10-05-next", "durum": "bekliyor"},
    ]}))
    # The performance file lags a day behind; it must not be what the watchdog trusts.
    (tmp_path / "video-performans.json").write_text(json.dumps({"videos": {
        "old": {"publishedAt": _iso(90)}}}))
    # Break caught: "no publish for 134h" while a video had gone live that morning.
    assert 2.9 <= float(_age(tmp_path)) <= 3.1


def test_falls_back_to_performance_file_then_unknown(tmp_path):
    (tmp_path / "video-performans.json").write_text(json.dumps({"videos": {
        "a": {"publishedAt": _iso(10)}}}))
    assert 9.9 <= float(_age(tmp_path)) <= 10.1
    (tmp_path / "video-performans.json").unlink()
    assert _age(tmp_path) == ""
