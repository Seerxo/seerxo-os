"""Automatic deploys must never cut across an active Stage C pilot."""
import os
import subprocess
from pathlib import Path


DEPLOY = Path(__file__).parents[1] / "scripts" / "deploy-company-os.sh"
COMPANYCTL = Path(__file__).parents[1] / "scripts" / "companyctl"
SHA = "a" * 40


def test_automatic_deploy_refuses_overrides_even_if_acknowledged_and_started_stage_c_runs():
    source = DEPLOY.read_text()
    automatic = source.index('automatic deployment refuses compose overrides')
    acknowledgement = source.index('[ "$found" = "$acked" ]')
    assert automatic < acknowledgement
    assert 'STAGE_C_RUN=$(q "select string_agg(id::text' in source
    assert 'Stage C AgentRun(s) are STARTED' in source
    assert 'companyctl stage-c status' in source


def test_stage_c_status_is_read_only_and_does_not_print_credentials(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text(f'''#!/usr/bin/env bash
set -eu
case "$*" in
  *"exec api printenv COMPANY_OS_GIT_SHA"*) echo {SHA} ;;
  *"exec worker printenv COMPANY_OS_GIT_SHA"*) echo {SHA} ;;
  *"exec worker printenv ENABLE_AUTONOMOUS_CODE_WRITE"*) echo true ;;
  *"inspect api"*) echo /opt/company-os/docker-compose.yml ;;
  *"inspect worker"*) echo /opt/company-os/docker-compose.yml,/opt/company-os/docker-compose.stage-c-pilot.yml ;;
  *"exec"*"worker python -c"*) echo '{{"api_sha": "{SHA}", "worker_sha": "{SHA}", "code_write_flag": true, "compose_overrides": ["worker:/opt/company-os/docker-compose.stage-c-pilot.yml"], "agents_paused": true, "acceptance_source_sha": "{SHA}", "runtime_readiness": {{"ready": true, "age_s": 12}}, "active_task_keys": ["TASK-0044"], "started_run_ids": [19]}}' ;;
  *) echo "unexpected docker call: $*" >&2; exit 9 ;;
esac
''')
    docker.chmod(0o755)
    result = subprocess.run(["bash", str(COMPANYCTL), "stage-c", "status"], text=True,
                            capture_output=True, env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}",
                            "API_CONTAINER": "api", "WORKER_CONTAINER": "worker"})
    assert result.returncode == 0, result.stderr
    assert '"code_write_flag": true' in result.stdout
    assert "docker-compose.stage-c-pilot.yml" in result.stdout
    assert "TASK-0044" in result.stdout and "19" in result.stdout
    assert "TOKEN" not in result.stdout and "SECRET" not in result.stdout


def test_authorize_and_revoke_are_root_only_and_touch_nothing_otherwise(tmp_path):
    import pytest
    if os.geteuid() == 0:
        pytest.skip("asserts the non-root refusal")
    docker = tmp_path / "docker"
    docker.write_text('#!/usr/bin/env bash\necho "docker must not be called: $*" >&2; exit 9\n')
    docker.chmod(0o755)
    for args, expected in ((["authorize", "TASK-0045"], "verified founder identity"), (["revoke"], "root on the host")):
        result = subprocess.run(["bash", str(COMPANYCTL), "stage-c", *args], text=True, capture_output=True,
                                env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}"})
        assert result.returncode == 1 and expected in result.stderr and "docker must not" not in result.stderr
