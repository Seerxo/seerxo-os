#!/usr/bin/env python3
"""Read-only check that an extracted release matches its archive's permissions.

    verify-release-permissions.py --root /opt/company-os --archive /tmp/src.tgz
    git -c tar.umask=0022 archive --format=tar SHA PATHS | ssh HOST python3 .../verify-release-permissions.py --root /opt/company-os --archive -

For every entry of the release archive it checks the extracted path under ROOT:
  - regular files and directories exist with the same type and exactly the archive's mode;
  - neither the archive nor the host gives group or world write;
  - symlinks are symlinks with the archive's target.
Exit 0 and "RELEASE PERMISSIONS: OK" when everything matches, 1 with one line per finding and
"RELEASE PERMISSIONS: FAIL" otherwise, 2 when the archive cannot be read or contains an unsafe path.
It never changes a mode, an owner or a file. Standard library only (the host already runs python3).
"""
import argparse
import os
import stat
import sys
import tarfile
from pathlib import PurePosixPath

OTHERS_WRITE = stat.S_IWGRP | stat.S_IWOTH


def unsafe(name: str) -> bool:
    p = PurePosixPath(name)
    return p.is_absolute() or ".." in p.parts


def writable(mode: int) -> str | None:
    if mode & stat.S_IWOTH:
        return "world-writable"
    if mode & stat.S_IWGRP:
        return "group-writable"
    return None


def check(root: str, archive: str) -> list[str]:
    findings = []
    if archive == "-":                       # streamed (e.g. `git archive ... | ssh host verify --archive -`)
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as tar:
            members = list(tar)
    else:
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
    if not members:
        raise ValueError("archive has no entries")
    for m in members:
        name = m.name.rstrip("/")
        if unsafe(name):
            raise ValueError(f"unsafe path in archive: {m.name!r}")
        path = os.path.join(root, name)
        if m.issym():
            if not os.path.islink(path):
                findings.append(f"{name}: not a symlink" if os.path.lexists(path) else f"{name}: missing")
            elif os.readlink(path) != m.linkname:
                findings.append(f"{name}: symlink to {os.readlink(path)}, archive says {m.linkname}")
            continue
        if not (m.isfile() or m.isdir()):
            findings.append(f"{name}: unexpected archive entry type")
            continue
        want = stat.S_IMODE(m.mode)
        if writable(want):
            findings.append(f"{name}: archive entry is {writable(want)} (mode {want:o})")
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            findings.append(f"{name}: missing")
            continue
        if stat.S_ISLNK(st.st_mode) or (m.isdir() != stat.S_ISDIR(st.st_mode)):
            findings.append(f"{name}: wrong type on host")
            continue
        have = stat.S_IMODE(st.st_mode)
        if writable(have):
            findings.append(f"{name}: {writable(have)} on host (mode {have:o})")
        if have != want:
            findings.append(f"{name}: mode {have:o}, archive says {want:o}")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--archive", required=True)
    args = ap.parse_args()
    try:
        findings = check(args.root, args.archive)
    except (tarfile.TarError, OSError, EOFError, ValueError) as error:
        print(f"verify-release-permissions: cannot verify: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # an uncaught exception would exit 1, which means "findings"
        print(f"verify-release-permissions: cannot verify: unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    for line in findings:
        print(f"  {line}")
    if findings:
        print(f"RELEASE PERMISSIONS: FAIL ({len(findings)} finding(s))")
        return 1
    print("RELEASE PERMISSIONS: OK (every release file and directory matches its archive mode; none group/world-writable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
