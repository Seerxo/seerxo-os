"""Container security identity and conservative recovery evidence; standard library only."""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


# What may differ between two runs of the same container without changing what it can do. Everything else in
# Config and HostConfig is identity. Hostname is the container ID, Image is a tag name (the ID is identity already),
# Labels are compose bookkeeping, Attach*/Tty/OpenStdin are console wiring. Env is identity but only as a digest.
VOLATILE_CONFIG = ("Hostname", "Image", "Labels", "Env", "AttachStdin", "AttachStdout", "AttachStderr", "Tty",
                   "OpenStdin", "StdinOnce")
VOLATILE_HOST_CONFIG = ("ContainerIDFile",)
# Lists whose order carries no meaning: sorted before hashing so a reordering never blocks a release.
SET_LIKE = {"Binds", "CapAdd", "CapDrop", "SecurityOpt", "Dns", "DnsOptions", "DnsSearch", "ExtraHosts", "GroupAdd",
            "Devices", "DeviceCgroupRules", "DeviceRequests", "VolumesFrom", "MaskedPaths", "ReadonlyPaths", "Links",
            "Ulimits", "Env"}


def _normalise(key, value):
    """None, [], {} and "" are the same absence; set-like lists are sorted; dict keys are canonical anyway."""
    if value in (None, [], {}, ""):
        return None
    if isinstance(value, dict):
        return {k: v for k, v in ((k, _normalise(k, v)) for k, v in value.items()) if v is not None} or None
    if isinstance(value, list):
        items = [_normalise(None, v) for v in value]
        return sorted(items, key=lambda v: canonical(v)) if key in SET_LIKE else items
    return value


def security_config(info: dict) -> str:
    """Digest of everything that decides what a container can do: all of Config and HostConfig except the short
    VOLATILE lists above, every mount (all fields), the names of the networks it is attached to (addresses are
    volatile), and the environment as a digest of the sorted NAME=value list (LD_PRELOAD, PYTHONPATH...). Values
    are never stored; only this combined digest is. A change of .env therefore changes identity (founder recovery)."""
    config, host_config = info.get("Config") or {}, info.get("HostConfig") or {}
    env = sorted(e for e in config.get("Env") or [] if isinstance(e, str))
    doc = {"Config": _normalise(None, {k: v for k, v in config.items() if k not in VOLATILE_CONFIG}),
           "HostConfig": _normalise(None, {k: v for k, v in host_config.items() if k not in VOLATILE_HOST_CONFIG}),
           "Mounts": sorted((_normalise(None, m) for m in info.get("Mounts") or [] if isinstance(m, dict)),
                            key=lambda m: canonical(m)),
           "Networks": sorted(((info.get("NetworkSettings") or {}).get("Networks") or {})),
           "Env": hashlib.sha256(canonical(env)).hexdigest()}
    return hashlib.sha256(canonical(doc)).hexdigest()


SERVICES = {"api": "company-api", "worker": "company-worker", "reasoner": "company-reasoner",
            "egress": "company-reasoner-egress"}
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def read(command, *, input=None):
    return subprocess.run(command, input=input, capture_output=True, check=True, timeout=120).stdout


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def recovery_state(sha, user, database):
    infos = json.loads(read(["docker", "inspect", *SERVICES.values()]))
    if not isinstance(infos, list) or len(infos) != len(SERVICES):
        raise ValueError("four container identities are required")
    by_name = {row.get("Name", "").removeprefix("/"): row for row in infos if isinstance(row, dict)}
    if set(by_name) != set(SERVICES.values()):
        raise ValueError("container names are missing or duplicated")
    images, configs, tags = {}, {}, []
    for component, name in SERVICES.items():
        row = by_name[name]
        if (not IMAGE.fullmatch(row.get("Image", "")) or not isinstance(row.get("Config"), dict)
                or not isinstance(row.get("HostConfig"), dict) or not isinstance(row.get("Mounts"), list)
                or not isinstance(row.get("NetworkSettings", {}).get("Networks"), dict)
                or row.get("State", {}).get("Running") is not True):
            raise ValueError("a container identity or running state is unavailable")
        if component in ("api", "worker"):
            refs = [e.removeprefix("COMPANY_OS_GIT_SHA=") for e in row["Config"].get("Env", [])
                    if e.startswith("COMPANY_OS_GIT_SHA=")]
            if refs != [sha]:
                raise ValueError("the API or worker source SHA differs")
        images[component], configs[component] = row["Image"], security_config(row)
        ref = row["Config"].get("Image", "")
        prefix = "company-os-" + component + ":"
        tags.append(ref.removeprefix(prefix) if ref.startswith(prefix) else None)
    prefix = ["docker", "exec", "company-postgres"]
    revision = read([*prefix, "psql", "-U", user, "-d", database, "-Atc",
                     "select version_num from alembic_version"]).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{12}", revision):
        raise ValueError("PostgreSQL has no single verified schema revision")
    schema = read([*prefix, "pg_dump", "-U", user, "-d", database, "--schema-only", "--no-owner", "--no-privileges"])
    # PostgreSQL randomizes these psql guard tokens on each dump; they are not schema contents.
    schema = b"\n".join(line for line in schema.splitlines()
                        if not line.startswith((b"\\restrict ", b"\\unrestrict ")))
    if not schema:
        raise ValueError("the PostgreSQL schema dump is empty")
    return {"version": 1, "sha": sha, "images": images, "configs": configs, "revision": revision,
            "schema": hashlib.sha256(schema).hexdigest(),
            "compose_tag": tags[0] if len(set(tags)) == 1 and tags[0]
            and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tags[0]) else None}


def recovery_check(mode, directory, sha, user, database):
    if mode not in ("snapshot", "verify") or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid recovery operation or source SHA")
    directory = Path(directory)
    if mode == "verify" and (directory / "migration-attempted").exists():
        raise ValueError("database migration was attempted; manual database recovery evidence is required")
    path, dump = directory / "recovery-identity.json", directory / "db.dump"
    current = recovery_state(sha, user, database)
    current["dump_sha256"] = file_digest(dump)
    if mode == "snapshot":
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(canonical(current).decode() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        for parent in (directory, directory.parent):
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1 << 20:
        raise ValueError("recovery identity is not a bounded regular file")
    expected = json.loads(path.read_bytes())
    if expected != current:
        raise ValueError("source, four images/configurations, schema, or backup identity differs")
    with dump.open("rb") as stream:
        subprocess.run(["docker", "exec", "-i", "company-postgres", "pg_restore", "--file=/dev/null"],
                       stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120)


def mark_migration(directory, before, after):
    if not all(re.fullmatch(r"[0-9a-f]{12}", value) for value in (before, after)) or before == after:
        raise ValueError("migration marker needs two distinct exact revisions")
    directory = Path(directory)
    with (directory / "migration-attempted").open("x") as stream:
        stream.write(before + " " + after + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 5 and sys.argv[1] == "migration":
            mark_migration(*sys.argv[2:])
        elif len(sys.argv) == 6:
            recovery_check(*sys.argv[1:])
        else:
            raise ValueError("usage: snapshot|verify BACKUP SHA PG_USER PG_DATABASE")
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"RECOVERY IDENTITY UNVERIFIED: {type(error).__name__}: "
              + (str(error) if isinstance(error, ValueError) else "evidence unavailable"), file=sys.stderr)
        sys.exit(1)
