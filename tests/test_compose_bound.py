"""Maintenance validates its effective config and holds the same locks as a release for the entire command."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


PILOT = {"services": {"company-worker": {"environment": {"ENABLE_AUTONOMOUS_CODE_WRITE": "true"}}}}


@pytest.fixture
def maintenance():
    with tempfile.TemporaryDirectory(prefix=".compose-test-", dir=ROOT) as directory:
        root = Path(directory)
        (root / "ledger").mkdir()
        (root / "builds").mkdir()
        (root / "bin").mkdir()
        (root / "bin/id").write_text("#!/bin/sh\necho 0\n")
        (root / "bin/id").chmod(0o755)
        docker = root / "bin/docker"
        docker.write_text(f"#!{sys.executable}\n" + '''import json, os, sys, time
from pathlib import Path
root = Path(os.environ['MAINTENANCE_TEST'])
with (root/'calls').open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
if 'config' in sys.argv:
    names = ('company-api','company-worker','company-reasoner','company-reasoner-egress')
    components = ('API','WORKER','REASONER','EGRESS')
    services = {n: {'image': os.environ['COMPANY_OS_IMAGE_'+c], 'environment': {'ENABLE_AUTONOMOUS_CODE_WRITE': 'false'}}
                for n,c in zip(names,components)}
    files = [sys.argv[i+1] for i, a in enumerate(sys.argv) if a == '-f']
    for extra in files[1:]:          # JSON is YAML: each later file merges per service, environment per key
        for name, svc in json.loads(Path(extra).read_text()).get('services', {}).items():
            merged = services.setdefault(name, {})
            for key, value in svc.items():
                if key == 'environment':
                    merged.setdefault('environment', {}).update(value)
                else:
                    merged[key] = value
    print(json.dumps({'services': services}))
if 'up' in sys.argv:
    (root/'started').touch()
    while (root/'block').exists(): time.sleep(0.01)
''')
        docker.chmod(0o755)
        ledger = root / "ledger/ledger.json"
        ledger.write_text(json.dumps({"bound_images": {c: "sha256:" + str(i) * 64
                          for i, c in enumerate(("api", "worker", "reasoner", "egress"), 1)}}))
        override = root / "pilot/stage-c-pilot.compose.yml"
        override.parent.mkdir(mode=0o755)
        override.write_text(json.dumps(PILOT))
        override.chmod(0o644)
        env = os.environ | {"COMPANY_OS_RELEASE_LEDGER": str(ledger), "MAINTENANCE_TEST": str(root),
                            "COMPANY_OS_STAGE_C_PILOT_OVERRIDE": str(override),
                            "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}
        yield root, env


def command(env, *args):
    return subprocess.run(["bash", str(ROOT / "scripts/compose-bound.sh"), *args], env=env,
                          text=True, capture_output=True, timeout=15)


def test_profile_and_pins_are_checked_for_the_command_that_is_executed(maintenance):
    root, env = maintenance
    result = command(env, "--profile", "stage-c", "up", "-d", "--no-deps", "--no-build", "company-egress")
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
    assert calls[0][:5] == ["compose", "--profile", "stage-c", "config", "--format"]
    assert calls[1][:4] == ["compose", "--profile", "stage-c", "up"]
    assert calls[1][calls[1].index("--pull") + 1] == "never"


@pytest.mark.parametrize("args", [("-f", "other.yml", "up"), ("up", "--build"), ("up", "--pull=always")])
def test_custom_configuration_rebuild_and_pull_cannot_bypass_the_check(maintenance, args):
    root, env = maintenance
    assert command(env, *args).returncode != 0
    assert not (root / "calls").exists()


def test_pending_release_prevents_any_docker_call(maintenance):
    root, env = maintenance
    path = root / "ledger/ledger.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"pending": "a" * 64}))
    assert command(env, "up", "--no-build", "company-worker").returncode != 0
    assert not (root / "calls").exists()


def test_install_and_build_locks_are_held_until_compose_finishes(maintenance):
    root, env = maintenance
    block = root / "block"
    block.touch()
    proc = subprocess.Popen(["bash", str(ROOT / "scripts/compose-bound.sh"), "up", "--no-build", "company-worker"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (root / "started").exists():
            assert proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        for path in (root / "install.lock", root / "builds/.lock"):
            with path.open("r+") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        block.unlink()
        proc.communicate(timeout=10)
    assert proc.returncode == 0


def test_the_pilot_override_arms_code_write_and_changes_nothing_else(maintenance):
    """A Stage C pilot recreates the worker with exactly one change: ENABLE_AUTONOMOUS_CODE_WRITE "true". The bound
    images are still checked on the configuration that is executed."""
    root, env = maintenance
    result = command(env, "--stage-c-pilot", "up", "-d", "--no-deps", "company-worker")
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
    override = env["COMPANY_OS_STAGE_C_PILOT_OVERRIDE"]
    assert calls[-1][:6] == ["compose", "-f", "docker-compose.yml", "-f", override, "up"]
    assert calls[-1][calls[-1].index("--pull") + 1] == "never"


@pytest.mark.parametrize("change", [
    {"company-worker": {"image": "sha256:" + "9" * 64}},                              # fights the binding
    {"company-worker": {"command": ["sh", "-c", "id"]}},                              # anything beyond the flag
    {"company-worker": {"environment": {"ENABLE_AUTONOMOUS_CODE_WRITE": "true", "GH_TOKEN": "x"}}},
    {"company-api": {"environment": {"ENABLE_AUTONOMOUS_CODE_WRITE": "true"}}},       # another service
    {"company-worker": {"environment": {"ENABLE_AUTONOMOUS_CODE_WRITE": "false"}}},   # arms nothing
])
def test_an_override_that_changes_anything_else_is_refused_before_compose_runs_it(maintenance, change):
    root, env = maintenance
    Path(env["COMPANY_OS_STAGE_C_PILOT_OVERRIDE"]).write_text(json.dumps({"services": change}))
    result = command(env, "--stage-c-pilot", "up", "-d", "--no-deps", "company-worker")
    assert result.returncode != 0 and "pilot override" in result.stderr
    assert not any(json.loads(line)[-1:] != ["json"] and "up" in json.loads(line)
                   for line in (root / "calls").read_text().splitlines())


@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_a_writable_pilot_override_is_refused_before_any_docker_call(maintenance, mode):
    root, env = maintenance
    Path(env["COMPANY_OS_STAGE_C_PILOT_OVERRIDE"]).chmod(mode)
    assert command(env, "--stage-c-pilot", "up", "company-worker").returncode != 0
    assert not (root / "calls").exists()


def test_the_pilot_flag_is_refused_for_rm(maintenance):
    root, env = maintenance
    assert command(env, "--stage-c-pilot", "rm", "-sf", "company-worker").returncode != 0
    assert not (root / "calls").exists()


@pytest.mark.parametrize("name", ["COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"])
def test_compose_environment_cannot_smuggle_another_configuration(maintenance, name):
    """`env COMPOSE_FILE=… compose-bound.sh up` bypassed the "custom config files are refused" rule."""
    root, env = maintenance
    assert command(env | {name: "/tmp/other.yml"}, "up", "company-worker").returncode != 0
    assert not (root / "calls").exists()
