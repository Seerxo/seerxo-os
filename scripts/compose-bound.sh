#!/usr/bin/env bash
# Host maintenance shares the release locks and checks the same effective configuration it executes.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "compose-bound: run as root (sudo)" >&2; exit 1; }
exec python3 -I - "${COMPANY_OS_RELEASE_LEDGER:-/var/lib/release-host/ledger/ledger.json}" "$@" <<'PYTHON'
import fcntl
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


try:
    ledger = Path(sys.argv[1])
    args, profiles = sys.argv[2:], []
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
        compose = ["docker", "compose", *profiles]
        config = json.loads(subprocess.check_output([*compose, "config", "--format", "json"], env=env))
        services = config.get("services") or {}
        for c, name in zip(components, ("company-api", "company-worker", "company-reasoner", "company-reasoner-egress")):
            if (services.get(name) or {}).get("image") != images[c]:
                raise ValueError("the effective Compose configuration differs from the bound images")
        result = subprocess.run([*compose, *args], env=env)
        sys.exit(result.returncode)
except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as error:
    print("compose-bound: " + (str(error) if isinstance(error, ValueError) else "evidence unavailable"), file=sys.stderr)
    sys.exit(1)
PYTHON
