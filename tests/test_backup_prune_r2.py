"""R2 retention failures must affect the backup result without stopping other targets."""
import re
import shlex
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup.sh"


def _prune_r2():
    return re.search(r"^prune_r2\(\) \{.*?^\}", SCRIPT.read_text(), re.S | re.M).group(0)


def _run(tmp_path, *, fail_prune=False, fail_usage=False):
    calls = tmp_path / "calls"
    logs = tmp_path / "logs"
    usage_command = "return 1" if fail_usage else "printf 'usage output\\n'; return 0"
    script = f"""
exec_log() {{ printf '%s\\n' "$*" >> {shlex.quote(str(logs))}; }}
r2() {{
  printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
  case "$1:$2" in
    prune:company-db/weekly) printf 'company output\\n'; {'return 1' if fail_prune else 'return 0'} ;;
    usage:*) {usage_command} ;;
  esac
}}
LOG=/dev/null
CLASS=weekly
R2_KEEP_DAILY=7
R2_KEEP_WEEKLY=4
R2_KEEP_MONTHLY=12
UPLOADED=(renders company-db)
FAILED=0
{_prune_r2()}
prune_r2
printf 'failed=%s\\n' "$FAILED"
exit "$FAILED"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"})
    return result, calls.read_text().splitlines(), logs.read_text().splitlines()


def test_failed_r2_prune_marks_backup_failed_but_continues_other_targets(tmp_path):
    result, calls, logs = _run(tmp_path, fail_prune=True)

    assert result.returncode == 1
    assert result.stdout.strip() == "failed=1"
    assert calls == ["prune company-db/weekly 4", "usage"]
    assert any("R2 prune failed for company-db/weekly" in line for line in logs)
    assert any("R2 prune skipped for renders" in line for line in logs)


def test_failed_r2_usage_query_marks_backup_failed(tmp_path):
    result, calls, logs = _run(tmp_path, fail_usage=True)

    assert result.returncode == 1
    assert result.stdout.strip() == "failed=1"
    assert calls[-1] == "usage"
    assert any("R2 usage query failed" in line for line in logs)
