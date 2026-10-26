"""Generated snapshots must carry enough provenance to reject stale fallback state."""
import os
import re
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "companyctl"


def test_snapshot_marks_its_freshness_deadline_and_runtime_provenance(tmp_path):
    fake = tmp_path / "docker"
    fake.write_text(r'''#!/usr/bin/env bash
set -eu
args="$*"
case "$args" in
  *"/health"*) exit 0 ;;
  *"/company/brief"*) printf '%s\n' 'COMPANY DAILY BRIEF' ;;
  *"/company/boot"*) printf '%s\n' '{"status":"ok"}' ;;
  *"/phases"*) printf '%s\n' '[{"number":1,"name":"Foundation","status":"COMPLETE","deliverables":"state"}]' ;;
  *) echo "unexpected docker command: $args" >&2; exit 9 ;;
esac
''')
    fake.chmod(0o755)
    env = os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}", "REPO_DOCS": str(tmp_path / "docs")}
    result = subprocess.run(["bash", str(SCRIPT), "snapshot"], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr

    snapshot = (tmp_path / "docs" / "CURRENT_STATE.md").read_text()
    assert re.search(r"Generated at UTC: `\d{4}-\d\d-\d\dT", snapshot)
    assert "STALE AFTER: 24 hours" in snapshot
    assert "Live API/worker provenance was unavailable when generated." in snapshot
    assert "COMPANY DAILY BRIEF" in snapshot

    phases = (tmp_path / "docs" / "PHASE_STATUS.md").read_text()
    assert "STALE AFTER: 24 hours" in phases
    assert "| 1 | Foundation | COMPLETE | state |" in phases
