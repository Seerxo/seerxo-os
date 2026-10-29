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
    print(json.dumps({'services': {n: {'image': os.environ['COMPANY_OS_IMAGE_'+c]} for n,c in zip(names,components)}}))
if 'up' in sys.argv:
    (root/'started').touch()
    while (root/'block').exists(): time.sleep(0.01)
''')
        docker.chmod(0o755)
        ledger = root / "ledger/ledger.json"
        ledger.write_text(json.dumps({"bound_images": {c: "sha256:" + str(i) * 64
                          for i, c in enumerate(("api", "worker", "reasoner", "egress"), 1)}}))
        env = os.environ | {"COMPANY_OS_RELEASE_LEDGER": str(ledger), "MAINTENANCE_TEST": str(root),
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
