#!/usr/bin/env python3
"""Founder-run stage/install of immutable release bundles. Never applies a release or changes the ledger."""
import ast
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST_FILES = ("app/__init__.py", "app/release_gateway.py", "app/release_host.py", "app/release_identity.py",
              "scripts/deploy-company-os.sh")
GATEWAY_FILES = ("app/__init__.py", "app/release_gateway.py", "app/release_gateway_service.py")
HOSTLIB = Path("/usr/local/lib/release-host")
GATEWAYLIB = Path("/usr/local/lib/release-gateway")
CONFIG = Path("/etc/release-host/config.json")
LEDGER = Path("/var/lib/release-host/ledger/ledger.json")
TIMERS = ("release-host.timer", "release-gateway.timer")


def trusted(path):
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise ValueError("bundle installation path is not owner-only writable")


def configured_clone(path):
    trusted(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o022 or info.st_size > 1 << 20):
        raise ValueError("untrusted release host configuration")
    doc = json.loads(path.read_bytes())
    if not isinstance(doc, dict) or not isinstance(doc.get("clone"), str) or not Path(doc["clone"]).is_absolute():
        raise ValueError("the active host configuration names no clone")
    clone = Path(doc["clone"])
    trusted(clone)
    return clone


def stage_bundle(clone, sha, library, *, gateway=False):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("a full source SHA is required")
    trusted(clone)
    trusted(library.parent)
    library.mkdir(mode=0o755, exist_ok=True)
    trusted(library)
    versions = library / "bundles"
    versions.mkdir(mode=0o755, exist_ok=True)
    trusted(versions)
    root = Path(tempfile.mkdtemp(prefix=sha[:12] + "-", dir=versions))
    os.chmod(root, 0o755)
    files = {}
    for name in GATEWAY_FILES if gateway else HOST_FILES:
        data = subprocess.check_output(["git", "--no-replace-objects", "-C", str(clone), "show", sha + ":" + name])
        target = root / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
        files[name] = hashlib.sha256(data).hexdigest()
    tree = ast.parse((root / "app/release_gateway.py").read_bytes())
    version = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "BUNDLE_VERSION" for t in n.targets))
    (root / "BUNDLE.json").write_text(json.dumps({"bundle_version": version, "source_sha": sha, "files": files}) + "\n")
    (root / "BUNDLE.json").chmod(0o644)
    # Isolated import checks the exact staged files; host staging has no operational/ledger authority.
    check = ("from app import release_gateway_service" if gateway else
             "from app import release_host; p=release_host.bundle_problem(allow_staged=True); assert p is None,p")
    subprocess.run([sys.executable, "-I", "-c", "import sys;sys.path.insert(0,sys.argv[1]);" + check, str(root)],
                   check=True, capture_output=True)
    return root


def point(library, root):
    link = library / "current.new"
    if link.exists() or link.is_symlink():
        raise ValueError("an unfinished bundle switch requires inspection")
    link.symlink_to(root)
    os.replace(link, library / "current")
    # Existing systemd WorkingDirectory=<library> remains valid; app resolves into the checked immutable bundle.
    app = library / "app"
    if app.exists() and not app.is_symlink():
        os.rename(app, library / ("app.pre-bundle-" + root.name))
    new = library / "app.new"
    if new.exists() or new.is_symlink():
        raise ValueError("an unfinished compatibility link requires inspection")
    new.symlink_to("current/app")
    os.replace(new, app)


def install(sha, clone, *, run=subprocess.run):
    active = [timer for timer in TIMERS if run(["systemctl", "is-active", "--quiet", timer]).returncode == 0]
    run(["systemctl", "stop", *TIMERS], check=True)
    # Timers stay stopped on every failure. Flat/manual commands do not yet share the install lock.
    deadline = time.monotonic() + 30
    while any(run(["systemctl", "is-active", "--quiet", service]).returncode == 0
              for service in ("release-host.service", "release-gateway.service")):
        if time.monotonic() >= deadline:
            raise ValueError("release services did not drain; timers remain stopped")
        time.sleep(0.2)
    probe = run(["pgrep", "-f", "app[.]release_(host|gateway_service)"])
    if probe.returncode != 1:
        raise ValueError("manual release commands are running or cannot be checked; timers remain stopped")
    trusted(LEDGER.parent.parent)
    fd = os.open(LEDGER.parent.parent / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ValueError("untrusted install lock")
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("release commands did not drain; timers remain stopped") from None
                time.sleep(0.1)
        trusted(LEDGER.parent)
        info = LEDGER.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_size > 1 << 20):
            raise ValueError("untrusted release ledger")
        raw = LEDGER.read_bytes()
        state = json.loads(raw)
        if not isinstance(state, dict) or state.get("last_sha") != sha or state.get("pending"):
            raise ValueError("the exact source SHA must already be applied, with no pending release")
        host = stage_bundle(clone, sha, HOSTLIB)
        gateway = stage_bundle(clone, sha, GATEWAYLIB, gateway=True)
        builds = LEDGER.parent.parent / "builds"
        builds.mkdir(mode=0o750, exist_ok=True)
        trusted(builds)
        os.chown(builds, os.geteuid(), LEDGER.parent.stat().st_gid)
        os.chmod(builds, 0o2750)
        # Exercise the real service principal's read permission, without any credential or secret data.
        probe_path = builds / (".read-probe-" + host.name)
        with probe_path.open("x") as stream:
            stream.write("bundle-read-probe\n")
        os.chown(probe_path, os.geteuid(), builds.stat().st_gid)
        probe_path.chmod(0o640)
        try:
            run(["runuser", "-u", "release-gateway", "--", "test", "-r", str(probe_path)], check=True)
            if run(["runuser", "-u", "release-gateway", "--", "test", "-w", str(probe_path)]).returncode != 1:
                raise ValueError("gateway must read but cannot write build records")
        finally:
            probe_path.unlink()
        if LEDGER.read_bytes() != raw:
            raise ValueError("the ledger changed during installation; timers remain stopped")
        point(HOSTLIB, host)
        point(GATEWAYLIB, gateway)
        run([sys.executable, "-I", "-c", "import sys;sys.path.insert(0,sys.argv[1]);"
             "from app import release_host; p=release_host.bundle_problem(); assert p is None,p", str(host)], check=True)
    finally:
        os.close(fd)
    for timer in active:
        run(["systemctl", "start", timer], check=True)
    return host


if __name__ == "__main__":
    try:
        if os.geteuid() != 0 or len(sys.argv) != 3 or sys.argv[1] not in ("stage", "install"):
            raise ValueError("run as root: install-release-bundle.py stage|install FULL_SHA")
        sha = sys.argv[2]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("a full source SHA is required")
        clone = configured_clone(CONFIG)
        root = (stage_bundle(clone, sha, Path("/usr/local/lib/release-host-next"))
                if sys.argv[1] == "stage" else install(sha, clone))
        print(root)
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
        print("BUNDLE BLOCKED: " + (str(error) if isinstance(error, ValueError) else "installation evidence unavailable"),
              file=sys.stderr)
        sys.exit(1)
