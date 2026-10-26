"""The release gate must reject unhealthy post-switch container facts."""
import os
import subprocess
from pathlib import Path

import pytest


GATE = Path(__file__).parents[1] / "scripts" / "deploy-health-gate.sh"
SHA = "a" * 40


def fake_docker(tmp_path, *, api_health="healthy", api_sha=SHA, worker_sha=SHA, denied="000"):
    docker = tmp_path / "docker"
    docker.write_text(f'''#!/usr/bin/env bash
set -eu
args="$*"
case "$args" in
  *"inspect company-api"*) printf '%s\\n' '{api_health}' ;;
  *"inspect company-reasoner-egress"*) printf '%s\\n' true ;;
  *"exec company-api printenv"*) printf '%s\\n' '{api_sha}' ;;
  *"exec company-worker printenv"*) printf '%s\\n' '{worker_sha}' ;;
  *"exec company-api curl"*) printf '%s\\n' '{{"worker":{{}}}}' ;;
  *"exec company-worker python"*) exit 0 ;;
  *"exec company-reasoner curl -fsS"*) printf '%s\\n' '{{"claude":{{}},"codex":{{}}}}' ;;
  *"api.anthropic.com"*) printf 404 ;;
  *"example.com"*) printf {denied} ;;
  *) echo "unexpected docker call: $args" >&2; exit 9 ;;
esac
''')
    docker.chmod(0o755)
    return docker


def run_gate(tmp_path, **facts):
    env = os.environ | {"DOCKER": str(fake_docker(tmp_path, **facts))}
    return subprocess.run(["bash", str(GATE), SHA], env=env, text=True, capture_output=True)


def test_release_gate_accepts_all_expected_post_switch_facts(tmp_path):
    result = run_gate(tmp_path)
    assert result.returncode == 0
    assert f"RELEASE HEALTH PASSED: {SHA}" in result.stdout


@pytest.mark.parametrize("facts", [
    {"api_health": "unhealthy"},
    {"api_sha": "b" * 40},
    {"worker_sha": "b" * 40},
    {"denied": "200"},
])
def test_release_gate_refuses_a_bad_post_switch_fact(tmp_path, facts):
    result = run_gate(tmp_path, **facts)
    assert result.returncode == 1
    assert "RELEASE HEALTH FAILED:" in result.stderr
