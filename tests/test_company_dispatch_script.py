"""The laptop hand-off must carry the server-built context contract."""
import json
import os
import subprocess
from pathlib import Path


def test_dry_run_command_carries_context_acceptance(tmp_path):
    # Break caught: app.dispatch grows a required context step while the shell hand-off's
    # duplicated argv builder silently omits it.
    plan = {
        "task": "TASK-0042", "agent": "developer", "hctl_id": "TASK-0042",
        "title": "Fix API", "acceptance": "tests pass", "workflow": "quick",
        "risk_level": "LOW", "priority": "P2", "auto": True,
        "auto_blocked_by": None, "repository": "org/repo", "project": "repo",
        "scope": None, "notes": [],
        "context_command": "ssh company-os-host sudo companyctl context TASK-0042",
    }
    bin_dir = tmp_path / "bin"
    repo = tmp_path / "projects" / "group" / "repo"
    bin_dir.mkdir()
    repo.mkdir(parents=True)
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(plan) + "'\n")
    ssh.chmod(0o755)
    hctl = bin_dir / "hctl"
    hctl.write_text("#!/bin/sh\nexit 0\n")
    hctl.chmod(0o755)

    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMPANY_PROJECTS_ROOT": str(tmp_path / "projects"),
    }
    result = subprocess.run(
        ["bash", "scripts/company-dispatch", "TASK-0042"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--acceptance=Load task context: ssh company-os-host sudo companyctl " \
           "context TASK-0042" in result.stdout
