#!/usr/bin/env bash
# Host maintenance shares the release locks and checks the same effective configuration it executes.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "compose-bound: run as root (sudo)" >&2; exit 1; }
exec python3 -I - "${COMPANY_OS_RELEASE_LEDGER:-/var/lib/release-host/ledger/ledger.json}" "$@" <<'PYTHON'
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def locked(path, shared=False):
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise ValueError("untrusted release lock directory")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.geteuid()):
            raise ValueError("untrusted release lock file")
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("a release command holds the lock; retry later") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


# The founder's Stage C pause fence: the one Compose overlay the running company-api keeps. The deploy driver re-applies
# it (deploy-company-os.sh FENCE_OVERLAY/FENCE_SHA256); maintenance must too, or recreating the API from the base file
# silently drops the kill switch. Same pin as the driver, checked against ops/ by a test.
FENCE_OVERLAY = Path("ops/company-coder-pause.compose.yml")
FENCE_SHA256 = "e5fa3c821ecd7f4da58b24275fb65592cd2d62072b679cfdddd6504bdd88e3fa"


def api_fence_live():
    out = subprocess.run(["docker", "inspect", "--type", "container", "--format",
                          "{{range .Config.Env}}{{println .}}{{end}}", "company-api"], capture_output=True, text=True)
    if out.returncode != 0:
        raise ValueError("cannot read company-api, so whether its pause fence is live is unknown")
    return "CODER_PAUSE_FENCE_ENABLED=true" in out.stdout.splitlines()


def fence_overlay_verified():
    for path, kind in ((FENCE_OVERLAY.parent, stat.S_ISDIR), (FENCE_OVERLAY, stat.S_ISREG)):
        info = path.lstat()   # lstat: a symlink is neither, so it is refused
        if not kind(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            return False
    return hashlib.sha256(FENCE_OVERLAY.read_bytes()).hexdigest() == FENCE_SHA256


try:
    ledger = Path(sys.argv[1])
    args, profiles = sys.argv[2:], []
    if any(name in os.environ for name in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES")):
        raise ValueError("Compose configuration environment overrides are refused")
    pilot = bool(args and args[0] == "--stage-c-pilot")
    if pilot:
        args = args[1:]
        if args != ["up", "-d", "--no-deps", "company-worker"]:
            raise ValueError("pilot override only supports up -d --no-deps company-worker")
        override = Path("/root/stage-c-pilot.compose.yml")
        for parent in override.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise ValueError("untrusted pilot override directory")
        info = override.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022 or info.st_size > 4096):
            raise ValueError("untrusted pilot override file")
        # Arming applies the working copy's Compose file and .env as root: another account able to write either (or
        # this directory) would choose what the armed worker runs. Both must be root- or caller-owned and not writable
        # by group or other.
        here = Path.cwd()
        for parent in (here, *here.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise ValueError("untrusted Compose working directory")
        for name in ("docker-compose.yml", ".env"):
            info = (here / name).lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.geteuid())
                    or info.st_mode & 0o022):
                raise ValueError(f"untrusted {name} in the Compose working directory")
        expected = {"services": {"company-worker": {"environment": {"ENABLE_AUTONOMOUS_CODE_WRITE": "true"}}}}
        if json.loads(override.read_bytes()) != expected:
            raise ValueError("pilot override must contain only the worker code-write flag (JSON)")
    while args and args[0] == "--profile":
        if len(args) < 3:
            raise ValueError("--profile requires a name and a command")
        profiles += args[:2]
        args = args[2:]
    # These are the maintenance operations used by the wizard and password rotation; custom config files are refused.
    if not args or args[0] not in ("up", "rm", "config", "ps"):
        raise ValueError("supported commands: [--profile NAME] up|rm|config|ps; custom global options are refused")
    if args[0] == "up":
        if any(a == "--build" or a.startswith("--pull") for a in args[1:]):
            raise ValueError("maintenance cannot build or select a pull policy")
        args = ["up", "--no-build", "--pull", "never", *args[1:]]
    with ExitStack() as stack:
        stack.enter_context(locked(ledger.parent.parent / "install.lock", shared=True))
        stack.enter_context(locked(ledger.parent.parent / "builds" / ".lock"))
        info = ledger.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022 or info.st_size > 1 << 20):
            raise ValueError("untrusted release ledger")
        state = json.loads(ledger.read_bytes())
        if not isinstance(state, dict) or state.get("pending"):
            raise ValueError("a release is pending or the ledger is unreadable")
        images = state.get("bound_images")
        components = ("api", "worker", "reasoner", "egress")
        if (not isinstance(images, dict) or set(images) != set(components)
                or not all(isinstance(v, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in images.values())):
            raise ValueError("the ledger binds no complete running image set")
        env = os.environ | {"COMPANY_OS_IMAGE_" + c.upper(): images[c] for c in components}
        # Pin selection even when .env contains Compose control variables; service interpolation still reads .env.
        env |= {"COMPOSE_PROFILES": "", "COMPOSE_ENV_FILES": ""}
        compose = ["docker", "compose", "--env-file", ".env", "-f", "docker-compose.yml", *profiles]
        fence = args[0] in ("up", "config") and api_fence_live()
        if fence:
            try:
                verified = fence_overlay_verified()
            except OSError:
                verified = False
            if not verified:
                raise ValueError("the pause fence is live but its overlay is missing, changed or writable; refusing to "
                                 "recreate company-api without it")
            compose += ["-f", str(FENCE_OVERLAY)]
        if pilot:
            compose += ["-f", str(override)]
        config = json.loads(subprocess.check_output([*compose, "config", "--format", "json"], env=env))
        services = config.get("services") or {}
        for c, name in zip(components, ("company-api", "company-worker", "company-reasoner", "company-reasoner-egress")):
            if (services.get(name) or {}).get("image") != images[c]:
                raise ValueError("the effective Compose configuration differs from the bound images")
        result = subprocess.run([*compose, *args], env=env)
        if result.returncode == 0 and fence and args[0] == "up":
            live = subprocess.run(["docker", "exec", "company-api", "printenv", "CODER_PAUSE_FENCE_ENABLED"],
                                  capture_output=True, text=True)
            if live.stdout.strip() != "true":
                raise ValueError("the pause fence is not live on company-api after up; restore it before anything else")
        sys.exit(result.returncode)
except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as error:
    print("compose-bound: " + (str(error) if isinstance(error, ValueError) else "evidence unavailable"), file=sys.stderr)
    sys.exit(1)
PYTHON
