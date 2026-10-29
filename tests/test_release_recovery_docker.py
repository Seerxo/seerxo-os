"""Real image/configuration retention and PostgreSQL backup rehearsal on an ephemeral hosted runner.

Uses disposable test images and the production prepare/restore/identity functions. It does not exercise the
production application's provider health checks or authorize a production release.
"""
import io
import json
import os
import re
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from app import release_identity as identity

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/deploy-company-os.sh").read_text()
REMOTE = SOURCE[SOURCE.index("<<'REMOTE'\n") + len("<<'REMOTE'\n"):SOURCE.index("\nREMOTE\n")]
COMPONENTS = ("api", "worker", "reasoner", "egress")
OLD, NEW = "1" * 40, "2" * 40


def call(*args, input=None, check=True, env=None, cwd=None):
    result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=cwd, timeout=120)
    if check and result.returncode:
        pytest.fail(str(args[:3]) + ": " + result.stderr.decode(errors="replace")[-4000:])
    return result


def function(name):
    start = REMOTE.index("\n" + name + "() {") + 1
    first = REMOTE[start:REMOTE.index("\n", start)]
    if first.rstrip().endswith("}"):
        return first + "\n"
    return REMOTE[start:REMOTE.index("\n}\n", start) + 3]


def require_hosted_local_docker():
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "never run this on a persistent runner"
    assert not os.environ.get("DOCKER_CONTEXT"), "DOCKER_CONTEXT may override DOCKER_HOST; refuse it"
    endpoint = os.environ.get("DOCKER_HOST") or json.loads(call(
        "docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}").stdout)
    assert endpoint.startswith("unix://"), "remote Docker contexts are forbidden"


# A stand-in for the two docker reads the gates make, matching real Docker (checked on 29.5): `docker ps` prints a
# container's image ID truncated to 12 hex characters unless --no-trunc is given; `image inspect` prints the full ID.
FAKE_DOCKER = r"""#!/bin/bash
case "$1" in
  image) echo "${@: -1}" ;;
  images) cat "$REPOSITORIES" ;;
  ps) if [[ " $* " == *" --no-trunc "* ]]; then cat "$ANCHORS"; else sed -E 's/^sha256:(.{12}).*/\1/' "$ANCHORS"; fi ;;
esac
"""
BOUND = {c: "sha256:" + hex(i)[2:] * 64 for i, c in enumerate(COMPONENTS, start=10)}


def fake_docker(tmp_path, anchors, repositories=()):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/docker").write_text(FAKE_DOCKER)
    (tmp_path / "bin/docker").chmod(0o755)
    (tmp_path / "anchors").write_text("".join(a + "\n" for a in anchors))
    (tmp_path / "repositories").write_text("".join(r + "\n" for r in repositories))
    return os.environ | {"PATH": str(tmp_path / "bin") + ":" + os.environ["PATH"], "ANCHORS": str(tmp_path / "anchors"),
                         "REPOSITORIES": str(tmp_path / "repositories"),
                         **{"BOUND_" + c.upper(): image for c, image in BOUND.items()}}


# 20000 extra lines make the reader's early exit (grep -q) a SIGPIPE for the writer, which pipefail turns into failure.
@pytest.mark.parametrize("extra", [0, 20000])
def test_prebind_accepts_full_image_ids_held_by_anchors(tmp_path, extra):
    """#19: `docker ps` without --no-trunc never printed a full ID, so every bound apply aborted as unanchored."""
    env = fake_docker(tmp_path, [*BOUND.values(), *["sha256:" + "f" * 64] * extra])
    result = call("bash", "-c", "set -euo pipefail\n" + function("prebind_images") + "\nprebind_images", env=env,
                  check=False)
    assert result.returncode == 0, result.stdout.decode()


def test_prebind_still_refuses_an_unanchored_image(tmp_path):
    env = fake_docker(tmp_path, [image for c, image in BOUND.items() if c != "reasoner"])
    result = call("bash", "-c", "set -euo pipefail\n" + function("prebind_images") + "\nprebind_images", env=env,
                  check=False)
    assert result.returncode == 1 and b"reasoner image" in result.stdout and b"not anchored" in result.stdout


@pytest.mark.parametrize("extra", [0, 20000])
def test_a_repository_named_sha256_is_refused_however_long_the_listing(tmp_path, extra):
    """The same early-exit under pipefail turned this refusal into a pass: fail open on a long image list."""
    start = SOURCE.index('if [ "$BOUND" = yes ] && docker images')
    gate = SOURCE[start:SOURCE.index("\nfi\n", start) + 4]
    env = fake_docker(tmp_path, [], ["sha256", *["company-os-api"] * extra]) | {"BOUND": "yes"}
    result = call("bash", "-c", "set -euo pipefail\n" + gate + "echo PASSED", env=env, check=False)
    assert result.returncode == 1 and b"named like an image ID" in result.stdout


def test_remote_context_cannot_override_the_local_endpoint(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-context")
    with pytest.raises(AssertionError, match="DOCKER_CONTEXT"):
        require_hosted_local_docker()


@pytest.mark.skipif(os.environ.get("SEERXO_REAL_RECOVERY") != "1",
                    reason="real Docker/PostgreSQL rehearsal requires its isolated hosted-runner job")
def test_real_prepared_images_restore_four_identities_and_postgres(tmp_path, monkeypatch):
    require_hosted_local_docker()
    call("docker", "version")
    base = "postgres:18-alpine"
    call("docker", "image", "inspect", base)  # the job pulls this one test dependency explicitly
    tag = "seerxo-recovery-" + uuid.uuid4().hex[:12]
    names = dict(zip(COMPONENTS, (tag + "-" + c for c in COMPONENTS)))
    pg = tag + "-postgres"
    images, anchors = set(), {}
    label = "seerxo.recovery-drill=" + tag
    uid = call("docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", "--read-only",
               "--security-opt", "no-new-privileges", base, "id", "-u", "postgres").stdout.decode().strip()
    assert re.fullmatch(r"[0-9]+", uid)
    user, database = "release_test", "company_recovery_test"
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    backup = tmp_path / "backup"
    backup.mkdir()

    def sql(statement, db=database):
        return call("docker", "exec", pg, "psql", "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-Atc",
                    statement).stdout.decode().strip()

    def config():
        return {"services": {"company-" + ("reasoner-egress" if c == "egress" else c): {
            "container_name": names[c], "image": "${COMPANY_OS_IMAGE_" + c.upper() + ":?required}",
            "build": {"context": ".", "target": c}, "user": uid, "entrypoint": ["sleep", "600"], "command": [],
            "environment": {"COMPANY_OS_GIT_SHA": "${COMPANY_OS_GIT_SHA:-preparing}",
                            "WORKER_IMAGE": "${COMPANY_OS_IMAGE_WORKER:?required}"},
            "labels": {"seerxo.recovery-drill": tag}, "network_mode": "none", "read_only": True,
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 16,
            "mem_limit": "32m", "cpus": 0.05,
            "tmpfs": ["/tmp:size=16m", "/var/lib/postgresql:size=1m"],
            "healthcheck": {"test": ["CMD", "true"], "interval": "1s", "timeout": "1s", "retries": 3}}
            for c in COMPONENTS}}

    (compose_dir / "docker-compose.yml").write_text(json.dumps(config()))
    common = os.environ | {"COMPOSE_PROJECT_NAME": tag}

    def environment(ids, sha):
        return common | {"COMPANY_OS_GIT_SHA": sha, **{"COMPANY_OS_IMAGE_" + c.upper(): ids[c] for c in COMPONENTS}}

    def prepared(sha):
        release = tmp_path / "staged" / (sha + ".test0001")
        release.mkdir(parents=True)
        dockerfile = ("FROM " + base + " AS base\nLABEL " + label + "\n"
                      + "\n".join("FROM base AS " + c + "\nLABEL generation=" + sha + " component=" + c for c in COMPONENTS)
                      + "\n")
        members = {"Dockerfile": dockerfile.encode(), "docker-compose.yml": json.dumps(config()).encode(),
                   "app/release_identity.py": (ROOT / "app/release_identity.py").read_bytes()}
        with tarfile.open(release / "src.tgz", "w:gz") as archive:
            for name, data in members.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o644
                archive.addfile(member, io.BytesIO(data))
        script = "\n".join(function(n) for n in ("service_of", "compose_env", "inputs_digest", "prepare_build"))
        script = script.replace("/run/company-os-deploy", str(tmp_path / "staged"))
        result = call("bash", "-c", "set -euo pipefail\n" + script + "\nprepare_build",
                      env=common | {"SHA": sha, "ATTEMPT": "1", "RELEASE_DIR": str(release)})
        line = next(line for line in result.stdout.decode().splitlines() if line.startswith("BOUND "))
        record = dict(part.split("=", 1) for part in line.split()[1:])
        ids = {c: record[c] for c in COMPONENTS}
        for c, image in ids.items():
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", image)
            images.add(image)
            anchors["company-os-bound-" + record["inputs"][:12] + "-a1-" + c] = image
        return ids

    def healthy():
        deadline = time.monotonic() + 30
        while True:
            rows = json.loads(call("docker", "inspect", *names.values()).stdout)
            if all(row["State"].get("Health", {}).get("Status") == "healthy" for row in rows):
                return
            assert time.monotonic() < deadline, "test services did not become healthy"
            time.sleep(0.1)

    real_run = subprocess.run
    monkeypatch.setattr(identity, "SERVICES", names)
    monkeypatch.setattr(identity.subprocess, "run", lambda args, *a, **kw: real_run(
        [pg if arg == "company-postgres" else arg for arg in args], *a, **kw))
    try:
        call("docker", "run", "-d", "--name", pg, "--label", label, "--network", "none", "--user", uid,
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--memory", "384m",
             "--cpus", "0.5", "--pids-limit", "96", "--tmpfs", "/var/lib/postgresql:uid=" + uid + ",mode=0700",
             "--tmpfs", "/var/run/postgresql:uid=" + uid + ",mode=0770", "--tmpfs", "/tmp:mode=1777",
             "-e", "POSTGRES_USER=" + user, "-e", "POSTGRES_DB=" + database,
             "-e", "POSTGRES_PASSWORD=disposable-drill-only", base, "postgres", "-c", "listen_addresses=")
        deadline = time.monotonic() + 60
        while call("docker", "exec", pg, "pg_isready", "-U", user, "-d", database, check=False).returncode:
            assert time.monotonic() < deadline, "disposable PostgreSQL did not start"
            time.sleep(0.2)
        sql("CREATE TABLE alembic_version(version_num varchar(32)); INSERT INTO alembic_version VALUES ('aaaaaaaaaaaa');"
            "CREATE TABLE drill_data(value text); INSERT INTO drill_data VALUES ('preserved');")
        old, new = prepared(OLD), prepared(NEW)
        old_env, new_env = environment(old, OLD), environment(new, NEW)
        prebind = function("prebind_images") + "\nprebind_images"
        call("bash", "-c", "set -euo pipefail\n" + prebind,
             env=old_env | {"BOUND_" + c.upper(): old[c] for c in COMPONENTS})
        call("docker", "compose", "up", "-d", "--no-build", "--pull", "never", env=old_env, cwd=compose_dir)
        healthy()
        (backup / "db.dump").write_bytes(call("docker", "exec", pg, "pg_dump", "-U", user, "-d", database, "-Fc").stdout)
        identity.recovery_check("snapshot", backup, OLD, user, database)
        # Prove this backup restores data and revision in a separate throwaway DB, never over the running DB.
        call("docker", "exec", pg, "createdb", "-U", user, "restored_drill")
        call("docker", "exec", "-i", pg, "pg_restore", "--exit-on-error", "--no-owner", "-U", user,
             "-d", "restored_drill", input=(backup / "db.dump").read_bytes())
        assert sql("SELECT value FROM drill_data", "restored_drill") == "preserved"
        assert sql("SELECT version_num FROM alembic_version", "restored_drill") == "aaaaaaaaaaaa"
        call("docker", "compose", "up", "-d", "--no-build", "--pull", "never", env=new_env, cwd=compose_dir)
        healthy()
        # The never-started production anchors keep the old images even while the new generation is running.
        assert call("docker", "image", "rm", old["api"], check=False).returncode != 0
        with pytest.raises(ValueError):
            identity.recovery_check("verify", backup, OLD, user, database)
        mapping = "\n".join("    " + key + ") args+=(\"" + names[c] + "\") ;;" for c, key in zip(
            COMPONENTS, ("company-api", "company-worker", "company-reasoner", "company-reasoner-egress")))
        wrapper = '''docker() {
  if [ "$1" = inspect ]; then
    shift; local args=() arg
    for arg in "$@"; do case "$arg" in
''' + mapping + '''
      *) args+=("$arg") ;; esac; done
    command docker inspect "${args[@]}"
  else command docker "$@"; fi
}
'''
        restore = "\n".join(function(n) for n in ("restore_env", "restore_resolve", "compose_restore"))
        restore += "\n" + wrapper + "\nFENCE_KEEP=no\ncompose_restore \"$@\"\n"
        call("bash", "-c", "set -euo pipefail\n" + restore, "_", *[old[c] for c in COMPONENTS], "restore-drill",
             env=new_env | {"COMPANY_OS_GIT_SHA": OLD}, cwd=compose_dir)
        healthy()
        identity.recovery_check("verify", backup, OLD, user, database)
        assert sql("SELECT value FROM drill_data") == "preserved"
        restored = identity.recovery_state(OLD, user, database)
        assert restored["images"] == old
        sql("CREATE TABLE unexpected_schema_change(id integer)")
        with pytest.raises(ValueError):
            identity.recovery_check("verify", backup, OLD, user, database)
        identity.mark_migration(backup, "aaaaaaaaaaaa", "bbbbbbbbbbbb")
        with pytest.raises(ValueError, match="manual database recovery"):
            identity.recovery_check("verify", backup, OLD, user, database)
        print("RECOVERY_ENGINE_EVIDENCE=" + json.dumps({
            "checked_out_sha": call("git", "rev-parse", "HEAD").stdout.decode().strip(),
            "event_sha": os.environ.get("GITHUB_SHA"), "old_images": old, "new_images": new,
            "restored": restored, "healthy": True, "backup_restore_verified": True,
            "schema_drift_refused": True, "attempted_migration_refused": True}, sort_keys=True))
    finally:
        # Labels survive partial preparation too; remove only this random test namespace, never prune the daemon.
        owned_ids = call("docker", "ps", "-a", "--filter", "label=" + label, "--format", "{{.ID}}", check=False)
        containers = set((*names.values(), pg, *anchors, *owned_ids.stdout.decode().split()))
        for name in containers:
            found = call("docker", "inspect", name, check=False)
            if found.returncode:
                continue
            row = json.loads(found.stdout)[0]
            image = row["Image"]
            metadata = json.loads(call("docker", "image", "inspect", image).stdout)[0]
            owned = row["Config"].get("Labels", {}).get("seerxo.recovery-drill") == tag
            owned = owned or (name in anchors and image == anchors[name]
                              and metadata["Config"].get("Labels", {}).get("seerxo.recovery-drill") == tag)
            if owned:
                call("docker", "rm", "-f", "-v", name)
        owned_images = call("docker", "image", "ls", "--filter", "label=" + label, "--no-trunc", "-q", check=False)
        images.update(owned_images.stdout.decode().split())
        for image in images:
            call("docker", "image", "rm", image, check=False)
