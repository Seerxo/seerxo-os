"""Release acceptance on a disposable host: the REAL root-side deploy script, run end to end against a sandbox
(private /opt/company-os, fake docker with images, containers and an alembic revision), never against production.

Proves: images come from a clean copy of the release (removed files and revert commits cannot contaminate them,
host-only data stays on the host), a failed build/migration/deployment puts the previous release back (source,
images, host CLI, cron), an interrupted deploy is detectable and deterministically recoverable, and two deploys
cannot overlap. Authorization (founder dispatch, exact SHA, explicit migration revision) is unchanged and still
covered by tests/test_deploy_authorization.py.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.release_host_sim import ROOT, Host, Release, base_files, migration, trusted_sandbox_parent

V1, V2, V3 = "1" * 40, "2" * 40, "3" * 40
CRON1 = "# schedule v1\n30 3 * * *   root  /opt/company-os/scripts/backup.sh\n"
CRON2 = "# schedule v2\n45 3 * * *   root  /opt/company-os/scripts/backup.sh\n"


@pytest.fixture
def host():
    parent = trusted_sandbox_parent()
    if parent is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI needs a directory whose ancestors only root and the runner can write; skipping is forbidden")
        pytest.skip("no sandbox directory with a trusted ancestor chain")
    h = Host(parent)
    yield h
    h.cleanup()


def release_v1(**over):
    return Release(V1, base_files(**{"app/old_module.py": ("old\n", 0o644), "scripts/company-os.cron": (CRON1, 0o644), **over}))


def release_v2(**over):
    files = base_files(**{"app/main.py": ("print('v2')\n", 0o644), "app/new_module.py": ("new\n", 0o644),
                          "scripts/companyctl": ("#!/usr/bin/env bash\n# v2\nexit 0\n", 0o755),
                          "scripts/company-os.cron": (CRON2, 0o644)})
    files.update(over)
    return Release(V2, files)


def with_schema_change(release: Release) -> Release:
    files = dict(release.files)
    files["migrations/versions/bbbbbbbbbbbb_more.py"] = (migration("bbbbbbbbbbbb", "aaaaaaaaaaaa").encode(), 0o644)
    return Release(release.sha, files)


def running(host: Host, release: Release | None = None, **knobs):
    """A host that is running V1: files, host CLI link, cron, containers; plus host-only data and a stale file."""
    release = release or release_v1()
    host.install_files(release)
    host.init_docker(release, **knobs)
    (host.opt / "scripts" / "local-only.sh").write_text("# host only\n")
    (host.opt / "notes").mkdir(exist_ok=True)
    (host.opt / "notes" / "host.txt").write_text("keep me\n")
    (host.opt / "migrations" / "versions" / "cccccccccccc_stale.py").write_text(migration("cccccccccccc", "aaaaaaaaaaaa"))
    # the host CLI the previous deploy installed (what the script's rollback must put back)
    subprocess.run(["bash", str(host.opt / "scripts" / "install-host-cli.sh"), "install", "--backup", str(host.parent / "cli-seed"),
                    "--expect-sha256", __import__("hashlib").sha256((host.opt / "scripts" / "companyctl").read_bytes()).hexdigest(),
                    "--bin", str(host.cli_link), "--target", str(host.opt / "scripts" / "companyctl")], check=True,
                   capture_output=True)
    return release


def api_image_files(host: Host, tag="latest"):
    s = host.docker_state()
    return set(s["images"][s["tags"][f"company-os-api:{tag}"]]["files"])


def api_image_heads(host: Host):
    s = host.docker_state()
    return s["images"][s["tags"]["company-os-api:latest"]]["heads"]


# --------------------------------------------------------------------------- clean build context

def test_the_image_is_built_from_the_release_not_from_the_host_tree(host):
    v1 = running(host)
    out = host.deploy(release_v2())
    assert out.returncode == 0, out.stdout
    assert "RELEASE VERIFIED" in out.stdout and host.phase() == "VERIFIED"
    files = api_image_files(host)
    assert "app/new_module.py" in files and "app/old_module.py" not in files            # the release removed old_module.py
    assert "migrations/versions/cccccccccccc_stale.py" not in files                     # a leftover revision never ships
    assert api_image_heads(host) == ["aaaaaaaaaaaa"]                                    # one head: the release's own
    assert "scripts/local-only.sh" not in files and ".env" not in files and not any(f.startswith(("backups", "notes")) for f in files)
    # ... while the host keeps what is its own, and says so
    assert (host.opt / "scripts" / "local-only.sh").exists() and (host.opt / "notes" / "host.txt").read_text() == "keep me\n"
    assert (host.opt / ".env").exists() and (host.opt / "app" / "old_module.py").exists()
    assert "HOST-ONLY (kept on the host" in out.stdout and "cccccccccccc_stale.py" in out.stdout
    assert {n: c[0] for n, c in host.containers().items()} == {n: V2 for n in host.containers()}
    assert not (host.backups()[-1] / "build-context").exists()                          # the context is thrown away


def test_a_revert_commit_cannot_resurrect_or_keep_files(host):
    """v1 -> v2 (adds new_module.py, drops old_module.py) -> v3 (= v1's tree again, as a revert commit would be)."""
    running(host)
    assert host.deploy(release_v2()).returncode == 0
    v3 = Release(V3, release_v1().files)
    out = host.deploy(v3)
    assert out.returncode == 0, out.stdout
    files = api_image_files(host)
    assert "app/old_module.py" in files and "app/new_module.py" not in files            # exactly v3, not v2's leftovers
    assert (host.opt / "app" / "new_module.py").exists()                                # still on the host, never in the image
    assert api_image_files(host, V3) == files


def test_every_file_in_the_image_matches_the_manifest_byte_for_byte(host):
    running(host)
    rel = release_v2()
    assert host.deploy(rel).returncode == 0
    s = host.docker_state()
    img = s["images"][s["tags"]["company-os-api:latest"]]["files"]
    want = {p: __import__("hashlib").sha256(c).hexdigest() for p, (c, _) in host.sandboxed(rel).files.items()
            if p.split("/")[0] in ("app", "migrations", "scripts", "agents", "skills", "tests", "docs") or p in ("alembic.ini", "requirements.txt")}
    assert img == want


def test_a_payload_that_differs_from_its_manifest_never_builds(host):
    running(host)
    before = host.tree()

    def corrupt(rel_dir):
        lines = (rel_dir / "main.sha256").read_text().splitlines()
        lines[0] = "0" * 64 + lines[0][64:]
        (rel_dir / "main.sha256").write_text("\n".join(lines) + "\n")
    out = host.deploy(release_v2(), tamper=corrupt)
    assert out.returncode != 0 and "does not match the release manifest" in out.stdout
    assert host.tree() == before and host.phase() == "ABORTED_RESTORED"
    assert "build -q" not in host.log.read_text()


def test_every_dockerfile_copy_source_is_part_of_the_release_payload():
    paths = re.search(r'^PATHS="([^"]+)"', (ROOT / "scripts" / "deploy-company-os.sh").read_text(), re.M).group(1).split()
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    sources = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"] and not any(p.startswith("--from") for p in parts):
            sources += parts[1:-1]
    assert sources, "no COPY lines parsed"
    for src in sources:
        assert any(src == p or src.startswith(p.rstrip("/") + "/") for p in paths), f"{src} is copied into an image but not shipped in the payload"
        assert any(t == src or t.startswith(src.rstrip("/") + "/") for t in tracked), f"{src} is not tracked in git"


def test_executables_the_release_needs_keep_their_mode_in_the_payload():
    modes = {}
    for line in subprocess.run(["git", "ls-files", "-s", "scripts"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines():
        meta, path = line.split("\t")
        modes[path] = meta.split()[0]
    cron = (ROOT / "scripts" / "company-os.cron").read_text()
    invoked = set()
    for line in cron.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "company-os/scripts/" in line:
            invoked |= set(re.findall(r"/opt/company-os/(scripts/[\w./-]+)", line))
    assert invoked, "the schedule invokes nothing?"
    for path in invoked | {"scripts/companyctl", "scripts/deploy-health-gate.sh", "scripts/install-host-cli.sh"}:
        assert modes.get(path) == "100755", f"{path} must be executable in the archive (git mode {modes.get(path)})"
    assert 'tar -czf "$B/files-before.tgz"' in (ROOT / "scripts" / "deploy-company-os.sh").read_text()


# --------------------------------------------------------------------------- failed migration / build / unauthorized

def assert_untouched(host: Host, before_tree, before_containers, before_tags, v1_cli):
    assert host.tree() == before_tree                                                   # bytes and modes, new files removed again
    assert host.containers() == before_containers                                        # same images, same start times
    s = host.docker_state()
    assert {k: v for k, v in s["tags"].items() if k.endswith(":latest")} == before_tags
    assert os.readlink(host.cli_link) == v1_cli and host.cron.read_text() == CRON1
    assert s["rev"] == "aaaaaaaaaaaa"


def snapshot_state(host):
    s = host.docker_state()
    return (host.tree(), host.containers(), {k: v for k, v in s["tags"].items() if k.endswith(":latest")},
            os.readlink(host.cli_link))


def test_a_failed_migration_leaves_the_running_services_and_the_previous_release_in_place(host):
    running(host)
    before = snapshot_state(host)
    host.set_knobs(fail_migrate=True)
    out = host.deploy(with_schema_change(release_v2()), allow_migration="bbbbbbbbbbbb")
    assert out.returncode != 0 and "MIGRATION FAILED" in out.stdout + out.stderr
    assert_untouched(host, *before)
    assert host.phase() == "ROLLBACK_FAILED" and not (host.opt / "app" / "new_module.py").exists()
    assert not (host.opt / "migrations" / "versions" / "bbbbbbbbbbbb_more.py").exists()   # added by the release, removed again
    assert "docker compose up" not in " ".join(host.log.read_text().split("\n")) and "\nup " not in "\n" + host.log.read_text()


def test_a_failed_build_leaves_the_running_services_and_the_previous_release_in_place(host):
    running(host)
    before = snapshot_state(host)
    host.set_knobs(fail_build=True)
    out = host.deploy(release_v2())
    assert out.returncode != 0 and "docker build of company-api failed" in out.stdout
    assert_untouched(host, *before)
    assert host.phase() == "ABORTED_RESTORED" and not (host.backups()[-1] / "build-context").exists()


def test_the_compose_normalized_default_dockerfile_is_a_plain_build(host):
    # Break caught (2026-10-09, run 38006612991): real `docker compose config` adds dockerfile: Dockerfile to every
    # build, and the check treated that default as an unsupported option, so every deploy aborted before building.
    running(host)
    out = host.deploy(release_v2())
    assert out.returncode == 0, out.stdout
    assert "does not reproduce" not in out.stdout and host.phase() == "VERIFIED"


@pytest.mark.parametrize("build", [
    {"dockerfile": "Dockerfile.other", "target": "api"},          # a different Dockerfile would be silently ignored
    {"dockerfile": "Dockerfile", "target": "api", "args": {"X": "1"}},
    {"dockerfile": "Dockerfile"},                                   # no target
])
def test_a_build_option_the_clean_build_cannot_reproduce_still_stops_the_deploy(host, build):
    running(host)
    before = snapshot_state(host)
    host.set_knobs(compose_build=build)
    out = host.deploy(release_v2())
    assert out.returncode != 0 and "company-api has a build configuration the clean-context build does not reproduce" in out.stdout
    assert_untouched(host, *before)
    assert host.phase() == "ABORTED_RESTORED"


def test_a_pre_deploy_dump_that_does_not_read_back_stops_the_deploy_before_any_change(host):
    running(host)
    before = snapshot_state(host)
    host.set_knobs(bad_dump=True)
    out = host.deploy(release_v2())
    # Break caught: the deploy went on with a restore point that would not restore completely.
    assert out.returncode != 0 and "pre-deploy database dump does not read back completely" in out.stdout
    assert_untouched(host, *before)
    assert not (host.backups()[-1] / "phase").exists()   # stopped before the journal: nothing to recover


@pytest.mark.parametrize("allow", ["", "cccccccccccc", "bbbbbbbbbbbbb", "BBBBBBBBBBBB"])
def test_a_schema_change_needs_its_own_exact_authorization(host, allow):
    running(host)
    before = snapshot_state(host)
    out = host.deploy(with_schema_change(release_v2()), allow_migration=allow)
    assert out.returncode != 0
    if allow in ("bbbbbbbbbbbbb", "BBBBBBBBBBBB"):
        assert "ALLOW_MIGRATION" in out.stdout                                          # malformed: refused before the host is touched
        assert not host.backups()
    else:
        assert "is not authorized" in out.stdout and host.phase() == "ABORTED_RESTORED"
    assert_untouched(host, *before)
    assert "upgrade" not in host.log.read_text()


def test_the_exact_migration_authorization_lets_the_schema_change_run(host):
    running(host)
    out = host.deploy(with_schema_change(release_v2()), allow_migration="bbbbbbbbbbbb")
    assert out.returncode == 0, out.stdout
    assert host.docker_state()["rev"] == "bbbbbbbbbbbb" and host.phase() == "VERIFIED"


# --------------------------------------------------------------------------- failed deployment -> previous release restored

def test_a_failed_deployment_restores_source_images_cli_and_cron(host):
    running(host)
    before = snapshot_state(host)
    old_sha = {n: c[0] for n, c in host.containers().items()}
    host.set_knobs(unhealthy_for=[V2])
    out = host.deploy(release_v2())
    assert out.returncode != 0 and "ROLLBACK VERIFIED" in out.stdout and host.phase() == "ROLLED_BACK_VERIFIED"
    assert host.tree() == before[0]
    assert os.readlink(host.cli_link) == before[3] and host.cron.read_text() == CRON1
    assert {n: c[0] for n, c in host.containers().items()} == old_sha                    # every service is back on the old SHA
    s = host.docker_state()
    assert {k: v for k, v in s["tags"].items() if k.endswith(":latest")} == before[2]     # compose's default tags are the old images
    assert all(c["health"] == "healthy" for n, c in s["containers"].items())


def test_a_release_that_removes_the_schedule_does_not_touch_it_unless_verified(host):
    running(host)
    no_cron = release_v2()
    files = {p: v for p, v in no_cron.files.items() if p != "scripts/company-os.cron"}
    no_cron = Release(V2, files)
    host.set_knobs(unhealthy_for=[V2])
    assert host.deploy(no_cron).returncode != 0
    assert host.cron.read_text() == CRON1                                                # failed release: schedule untouched
    host.set_knobs()
    out = host.deploy(Release(V3, files))
    assert out.returncode == 0 and not host.cron.exists()                                # verified release without a schedule: removed


def test_a_verified_release_replaces_the_schedule_from_its_own_manifest(host):
    running(host)
    assert host.deploy(release_v2()).returncode == 0
    assert host.cron.read_text() == CRON2


def test_a_rollback_that_cannot_be_verified_blocks_every_later_deploy_until_recovered(host):
    running(host)
    host.set_knobs(unhealthy_for=[V2, V1])
    out = host.deploy(release_v2())
    assert out.returncode != 0 and "ROLLBACK FAILED" in out.stdout and host.phase() == "ROLLBACK_FAILED"
    blocked = host.backups()[-1]
    host.set_knobs()                                                                      # the platform is healthy again
    before = (host.tree(), host.containers())
    refused = host.deploy(Release(V3, release_v1().files))
    assert refused.returncode != 0 and "did not finish cleanly" in refused.stdout and blocked.name in refused.stdout
    assert (host.tree(), host.containers()) == before and len(host.backups()) == 1
    ro = host.deploy(Release(V3, release_v1().files), apply=False)
    assert ro.returncode == 0 and "UNFINISHED" in ro.stdout and "nothing changed" in ro.stdout
    rec = host.recover(blocked)
    assert rec.returncode == 0 and "RECOVERY VERIFIED" in rec.stdout and host.phase(blocked) == "ROLLED_BACK_VERIFIED"
    assert host.deploy(Release(V3, release_v1().files)).returncode == 0


# --------------------------------------------------------------------------- interrupted deploys

@pytest.mark.parametrize("crash, phase", [("build", "BUILDING"), ("migrate", "MIGRATING"), (f"switch:{V2}", "SWITCHING")])
def test_an_interrupted_deploy_is_visible_blocks_the_next_one_and_recovers_deterministically(host, crash, phase):
    running(host)
    before = (host.tree(), {n: c[0] for n, c in host.containers().items()}, os.readlink(host.cli_link), host.cron.read_text())
    host.set_knobs(crash=crash)
    out = host.deploy(with_schema_change(release_v2()), allow_migration="bbbbbbbbbbbb")
    assert out.returncode < 0                                                             # killed (SIGKILL), no cleanup ran
    backup = host.backups()[-1]
    assert host.phase(backup) == phase
    host.set_knobs()
    refused = host.deploy(release_v2())
    assert refused.returncode != 0 and "did not finish cleanly" in refused.stdout and len(host.backups()) == 1
    rec = host.recover(backup)
    if crash != "build":
        assert rec.returncode != 0 and "RECOVERY PARTIAL" in rec.stdout, rec.stdout
        assert "RECOVERY VERIFIED" not in rec.stdout and host.phase(backup) == "ROLLED_BACK_PARTIAL"
        assert "refusing to start previous code" in rec.stdout
        assert host.deploy(release_v2()).returncode != 0
        return
    assert rec.returncode == 0 and "RECOVERY VERIFIED: " + V1 in rec.stdout, rec.stdout
    assert host.phase(backup) == "ROLLED_BACK_VERIFIED"
    assert host.tree() == before[0]                                                       # bytes and modes; added files gone
    assert {n: c[0] for n, c in host.containers().items()} == before[1] and host.cron.read_text() == before[3]
    assert os.readlink(host.cli_link) == before[2]
    again = host.recover(backup)                                                          # idempotent
    assert again.returncode == 0 and "NOTHING TO RECOVER" in again.stdout
    # the host is deployable again; a crash after the migration left the schema at bbbb, which is then the current revision
    assert host.deploy(with_schema_change(release_v2()), allow_migration="bbbbbbbbbbbb").returncode == 0


def test_the_runbook_recovery_command_needs_no_deploy_mode(host):
    # Break caught (release review of 59789aa): `sudo RECOVER_DIR=... bash deploy-driver.sh` runs without AUTOMATIC
    # (sudo drops the environment) and aborted with "deploy mode unknown"; the harness pre-set AUTOMATIC and hid it.
    running(host)
    host.set_knobs(crash="build")
    assert host.deploy(with_schema_change(release_v2()), allow_migration="bbbbbbbbbbbb").returncode < 0
    backup = host.backups()[-1]
    host.set_knobs()
    rec = host.recover(backup, AUTOMATIC=None, ALLOW_MIGRATION=None)
    assert rec.returncode == 0 and "RECOVERY VERIFIED: " + V1 in rec.stdout and "deploy mode unknown" not in rec.stdout, rec.stdout
    assert host.phase(backup) == "ROLLED_BACK_VERIFIED"


def test_a_normal_deploy_without_a_mode_is_still_refused(host):
    running(host)
    before = (host.tree(), {n: c[0] for n, c in host.containers().items()})
    out = host.deploy(release_v2(), automatic=None)
    assert out.returncode != 0 and "deploy mode unknown" in out.stdout
    assert (host.tree(), {n: c[0] for n, c in host.containers().items()}) == before
    out = host.deploy(release_v2(), automatic="")
    assert out.returncode != 0 and "deploy mode unknown" in out.stdout


def test_a_half_switched_host_is_really_mixed_before_recovery(host):
    running(host)
    host.set_knobs(crash=f"switch:{V2}")
    host.deploy(release_v2())
    shas = {n: c[0] for n, c in host.containers().items()}
    assert V2 in shas.values() and V1 in shas.values()                                    # the failure mode recovery exists for
    assert host.recover(host.backups()[-1]).returncode == 0
    assert set(c[0] for c in host.containers().values()) == {V1}


def test_recovery_refuses_anything_that_is_not_a_deploy_backup_with_a_journal(host):
    running(host)
    host.set_knobs(crash=f"switch:{V2}")
    host.deploy(release_v2())
    backup = host.backups()[-1]
    driver = backup / "deploy-driver.sh"
    for bad in (str(backup) + "/..", str(host.opt / "backups" / "deploy-x"), "/etc", str(host.opt)):
        out = host.recover_with(driver, bad) if hasattr(host, "recover_with") else subprocess.run(
            ["bash", str(driver)], env=host.env(RECOVER_DIR=bad), capture_output=True, text=True)
        assert out.returncode != 0 and "ABORT" in out.stdout + out.stderr
    (backup / "phase").unlink()
    out = host.recover(backup)
    assert out.returncode != 0 and "no deploy journal" in out.stdout
    assert V2 in {c[0] for c in host.containers().values()}                               # refused: nothing was touched


def test_a_tampered_journal_value_is_rejected_and_never_executed(host):
    running(host)
    host.set_knobs(crash=f"switch:{V2}")
    host.deploy(release_v2())
    backup = host.backups()[-1]
    marker = host.parent / "pwned"
    env_file = backup / "deploy.env"
    env_file.write_text(env_file.read_text() + f"API_IMG=$(touch {marker})\n")
    out = host.recover(backup)
    assert out.returncode != 0 and "malformed API_IMG" in out.stdout and not marker.exists()
    env_file.write_text("\n".join(l for l in env_file.read_text().splitlines() if "touch" not in l) + "\nOLD_SHA=zzz\n")
    assert host.recover(backup).returncode != 0 and not marker.exists()


# --------------------------------------------------------------------------- concurrency

def test_two_deploys_cannot_overlap_and_the_second_changes_nothing(host):
    running(host)
    gate = host.parent / "hold-build"
    gate.touch()
    host.set_knobs(block_build=str(gate))
    first = host.deploy(release_v2(), detach=True)
    assert first.returncode == 0 and "detached" in first.stdout
    deadline = time.monotonic() + 30
    while not Path(str(gate) + ".started").exists():
        assert time.monotonic() < deadline, "the first deploy never reached the build"
        time.sleep(0.05)
    during = (host.tree(), host.containers())
    second = host.deploy(Release(V3, release_v1().files), detach=True)
    assert second.returncode != 0 and "another deploy is running" in second.stdout
    assert len(host.backups()) == 1                                                       # the loser created nothing
    gate.unlink()                                                                         # let the first one finish
    log = host.var / "log" / "company-os-deploy-latest.log"
    deadline = time.monotonic() + 60
    while "EXIT " not in (log.read_text() if log.exists() else ""):
        assert time.monotonic() < deadline, "the first deploy never finished"
        time.sleep(0.1)
    assert log.read_text().rstrip().endswith("EXIT 0") and host.phase() == "VERIFIED"
    assert {c[0] for c in host.containers().values()} == {V2}
    assert during[1] == {n: c for n, c in during[1].items()}                              # (unchanged while it was blocked)


# --------------------------------------------------------------------------- B6 step 8: verify-release-manifest

REFUSED = "ABORT: verify-release-manifest: this host is bootstrapped"


def bootstrapped(host: Host, ledger: dict | str):
    """The ledger release_host keeps once the host is bootstrapped (dict -> JSON; a str is written verbatim)."""
    host.ledger.parent.mkdir(parents=True, exist_ok=True)
    host.ledger.write_text(ledger if isinstance(ledger, str) else json.dumps(ledger))


def snapshot(host: Host):
    host.log.write_text("")
    return host.tree(), {n: c[0] for n, c in host.containers().items()}


def unchanged_since(host: Host, before):
    """Nothing past the check ran: no docker call at all, no backup directory, same files and containers."""
    return host.log.read_text() == "" and host.backups() == [] and snapshot(host) == before


def next_dir(host: Host, sha: str) -> str:
    """The staged directory host.deploy(staged=True) will create next: release_host names it before marking pending."""
    return str(host.run_root / f"{sha}.sim{host.releases + 1:05d}")


def pending(sha, rel, **over):
    return {"last_sha": V1, "pending": "d" * 64, "pending_sha": sha, "pending_release_dir": rel,
            "pending_approver": "founder"} | over


@pytest.mark.parametrize("ledger, staged, extra", [
    (lambda h: {"last_sha": V1}, True, {}),                                          # bootstrapped, nothing pending
    (lambda h: pending("", next_dir(h, V2)), True, {}),                              # empty pending SHA
    (lambda h: pending(V3, next_dir(h, V2)), True, {}),                              # pending for another release
    (lambda h: pending(V2, next_dir(h, V2)), False, {}),                             # legacy transport, no staged dir
    (lambda h: pending(V2, next_dir(h, V2)), True, {"RELEASE_DIR": None}),           # RELEASE_DIR dropped
    (lambda h: pending(V2, str(h.run_root / f"{V2}.abcdefgh")), True, {}),           # another upload of the pending SHA
    (lambda h: {k: v for k, v in pending(V2, next_dir(h, V2)).items() if k != "pending"}, True, {}),  # no digest
    (lambda h: {k: v for k, v in pending(V2, next_dir(h, V2)).items() if k != "pending_release_dir"}, True, {}),
    (lambda h: pending(V3, next_dir(h, V2)), True, {"automatic": "true"}),           # the mode changes nothing
    (lambda h: pending(7, next_dir(h, V2)), True, {}),                               # malformed field
    (lambda h: "{not json", True, {}),                                               # malformed ledger
    (lambda h: "[]", True, {}),                                                      # not an object
], ids=["no-pending", "empty-pending", "other-sha", "legacy-transport", "no-release-dir", "other-upload-same-sha",
        "no-digest", "no-release-dir-binding", "automatic", "bad-field", "bad-json", "not-object"])
def test_a_bootstrapped_host_refuses_an_apply_the_verifier_did_not_verify(host, ledger, staged, extra):
    running(host)
    bootstrapped(host, ledger(host))
    before = snapshot(host)
    out = host.deploy(release_v2(), staged=staged, **extra)
    assert out.returncode != 0 and REFUSED in out.stdout, out.stdout
    assert unchanged_since(host, before)


def test_a_release_dir_staged_for_another_sha_is_refused(host):
    running(host)
    other = host.sandboxed(release_v2()).write(host.run_root / f"{V3}.abcdefgh")
    bootstrapped(host, pending(V2, str(other)))
    before = snapshot(host)
    out = host.deploy(release_v2(), RELEASE_DIR=str(other))
    assert out.returncode != 0 and REFUSED in out.stdout, out.stdout
    assert unchanged_since(host, before)


def test_an_unreadable_ledger_refuses(host):
    running(host)
    host.ledger.parent.mkdir(parents=True)
    host.ledger.symlink_to(host.parent / "missing")                      # dangling: present, unreadable
    before = snapshot(host)
    out = host.deploy(release_v2(), staged=True)
    assert out.returncode != 0 and REFUSED in out.stdout and "<unreadable-or-incomplete-ledger>" in out.stdout, out.stdout
    assert unchanged_since(host, before)


def test_the_release_the_verifier_marked_pending_applies(host):
    running(host)
    ids = host.prepare(release_v2())
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids))
    out = host.deploy(release_v2(), staged=True, automatic="true")
    assert out.returncode == 0 and "RELEASE VERIFIED: " + V2 in out.stdout and REFUSED not in out.stdout, out.stdout
    assert host.phase() == "VERIFIED" and {c[0] for c in host.containers().values()} == {V2}
    assert running_binding(host) == ids      # exactly the bound IDs, never a tag


# --- bound releases: the driver starts prebuilt IDs and never builds (IMAGE_BINDING_DESIGN.md §3–§5) -----------

def docs_release(base: Release, sha: str, text: str) -> Release:
    """`base` plus one docs file that no image copies: the shape policy v3 lets the gateway release."""
    return Release(sha, {**base.files, "docs/NOTES.md": (text, 0o644)})


def release_tags(state) -> dict:
    """Image tags a deploy may move; rollback-<time> anchors are added by every attempt, by design."""
    return {t: i for t, i in state["tags"].items() if ":rollback-" not in t}


def gateway_host(host: Host) -> Release:
    """V1 deployed by the real driver, then (as release_host's bootstrap did) recorded as the running binding."""
    v1 = running(host)
    assert host.deploy(v1).returncode == 0
    return v1


def v1_binding(host: Host) -> dict:
    """The four IDs the legacy deploy left running, in release_gateway shape."""
    return running_binding(host)


KINDS = {"company-api": "api", "company-worker": "worker", "company-reasoner": "reasoner",
         "company-reasoner-egress": "egress"}


def running_binding(host: Host) -> dict:
    """Component -> the image ID its container runs now (release_gateway shape)."""
    return {KINDS[name]: container[1] for name, container in host.containers().items() if name in KINDS}


def test_a_gateway_release_binds_the_prebuilt_images_and_applies(host):
    v1 = gateway_host(host)
    running_ids = v1_binding(host)
    ids = host.prepare(docs_release(v1, V2, "notes\n"))          # a docs-only release has the same image inputs
    assert ids == running_ids                                     # nothing was rebuilt for unreviewed bytes
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids, bound_images=running_ids,
                               pending_approver="gateway"))
    out = host.deploy(docs_release(v1, V2, "notes\n"), staged=True, automatic="true")
    assert out.returncode == 0 and "RELEASE VERIFIED: " + V2 in out.stdout, out.stdout
    assert running_binding(host) == ids


def test_a_bound_apply_never_builds_anything(host):
    gateway_host(host)
    v2 = release_v2()                                             # different app bytes: four new images
    ids = host.prepare(v2)
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids))
    host.log.write_text("")
    out = host.deploy(v2, staged=True, automatic="true")
    assert out.returncode == 0, out.stdout
    assert " build " not in " " + host.log.read_text() + " "      # no docker build was reachable from apply
    assert running_binding(host) == ids


def test_a_pending_release_without_images_is_not_the_bootstrap_release(host):
    v1 = gateway_host(host)
    running_ids = v1_binding(host)
    before = snapshot(host)
    bootstrapped(host, pending(V2, next_dir(host, V2), bound_images=running_ids))
    out = host.deploy(docs_release(v1, V2, "notes\n"), staged=True, automatic="true")
    assert out.returncode != 0 and "this pending release names none" in out.stdout, out.stdout
    assert snapshot(host) == before


def test_a_bound_apply_refuses_an_image_it_cannot_anchor(host):
    v1 = gateway_host(host)
    v2 = release_v2()
    ids = host.prepare(v2)
    state = host.docker_state()
    state["anchors"].pop(next(n for n in state["anchors"] if n.startswith("company-os-bound-") and n.endswith("-worker")))
    host.state.write_text(json.dumps(state))
    before = snapshot(host)
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids))
    host.log.write_text("")
    out = host.deploy(v2, staged=True, automatic="true")
    assert out.returncode != 0 and "not anchored" in out.stdout, out.stdout
    assert snapshot(host) == before and " upgrade " not in " " + host.log.read_text() + " "


def test_a_bound_apply_refuses_an_image_that_is_not_present(host):
    v1 = gateway_host(host)
    v2 = release_v2()
    ids = host.prepare(v2)
    state = host.docker_state()
    state["images"].pop(ids["reasoner"])
    host.state.write_text(json.dumps(state))
    before = snapshot(host)
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids))
    out = host.deploy(v2, staged=True, automatic="true")
    assert out.returncode != 0 and "is not present on this host" in out.stdout, out.stdout
    assert snapshot(host) == before


def test_a_bound_host_must_run_all_four_services(host):
    v1 = gateway_host(host)
    v2 = release_v2()
    ids = host.prepare(v2)
    state = host.docker_state()
    del state["containers"]["company-reasoner-egress"]
    host.state.write_text(json.dumps(state))
    before = snapshot(host)
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids))
    host.log.write_text("")
    out = host.deploy(v2, staged=True, automatic="true")
    assert out.returncode != 0 and "must run all four services" in out.stdout, out.stdout
    assert snapshot(host) == before and " upgrade " not in " " + host.log.read_text() + " "


def test_a_failed_bound_release_rolls_back_to_the_previous_four_images(host):
    v1 = gateway_host(host)
    running_ids = v1_binding(host)
    v2 = release_v2()
    ids = host.prepare(v2)
    host.set_knobs(unhealthy_for=[V2])
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids, bound_images=running_ids))
    out = host.deploy(v2, staged=True, automatic="true")
    assert out.returncode != 0 and "ROLLBACK VERIFIED" in out.stdout, out.stdout
    assert host.phase() == "ROLLED_BACK_VERIFIED"
    assert running_binding(host) == running_ids


def test_the_prepare_branch_builds_anchors_and_changes_nothing_else(host):
    running(host)
    host.log.write_text("")
    ids = host.prepare(release_v2())
    log = host.log.read_text()
    assert set(ids) == {"api", "worker", "reasoner", "egress"} and all(i.startswith("sha256:") for i in ids.values())
    assert len(re.findall(r"^build ", log, re.M)) == 4
    assert log.count("create --name company-os-bound-") == 4
    assert not re.search(r"^(tag|rename|rm|compose up|compose run)", log, re.M)   # no tag, no container, no migration
    assert {n: c[0] for n, c in host.containers().items()} == {n: V1 for n in host.containers()}


def test_without_a_ledger_the_attended_legacy_deploy_is_unchanged(host):
    running(host)
    assert not host.ledger.exists() and not host.ledger.is_symlink()
    out = host.deploy(release_v2())                                      # legacy transport, no RELEASE_DIR
    assert out.returncode == 0 and "RELEASE VERIFIED: " + V2 in out.stdout and REFUSED not in out.stdout, out.stdout


def test_a_read_only_run_on_a_bootstrapped_host_is_unchanged(host):
    running(host)
    bootstrapped(host, pending(V3, next_dir(host, V3)))
    out = host.deploy(release_v2(), apply=False)
    assert out.returncode == 0 and "read-only run; nothing changed" in out.stdout and REFUSED not in out.stdout, out.stdout


def test_recovery_on_a_bootstrapped_host_is_unchanged(host):
    running(host)
    host.set_knobs(crash="build")
    assert host.deploy(with_schema_change(release_v2()), allow_migration="bbbbbbbbbbbb").returncode < 0
    backup = host.backups()[-1]
    host.set_knobs()
    bootstrapped(host, pending(V3, next_dir(host, V3)))
    rec = host.recover(backup, APPLY="--apply")
    assert rec.returncode == 0 and "RECOVERY VERIFIED: " + V1 in rec.stdout and REFUSED not in rec.stdout, rec.stdout


def test_a_detached_unverified_apply_takes_no_lock_and_writes_no_log(host):
    running(host)
    bootstrapped(host, pending(V3, next_dir(host, V3)))
    before = snapshot(host)
    out = host.deploy(release_v2(), staged=True, detach=True)
    assert out.returncode != 0 and REFUSED in out.stdout and "detached" not in out.stdout, out.stdout
    assert unchanged_since(host, before)
    assert not list((host.var / "log").glob("company-os-deploy*")) and not list((host.var / "lock").iterdir())


def test_a_refusal_never_deletes_the_directory_it_was_handed(host):
    # The named directory may be a live transaction's (release_host's own staged copy): a refused caller that names it
    # must not remove it. release_host cleans up its own directory; nothing else does.
    running(host)
    live = host.sandboxed(release_v2()).write(host.run_root / f"{V2}.livetxn1")
    bootstrapped(host, pending(V2, str(host.run_root / f"{V2}.othertxn")))   # the live one is not the pending one
    out = host.deploy(release_v2(), RELEASE_DIR=str(live))
    assert out.returncode != 0 and REFUSED in out.stdout, out.stdout
    assert live.is_dir() and any(live.iterdir())


def test_nothing_sourced_after_the_gate_can_change_what_it_decided(host):
    running(host)
    bootstrapped(host, pending(V2, next_dir(host, V2)))
    env = host.opt / ".env"
    env.write_text(env.read_text() + f"SHA={V3}\n")                    # a root-sourced reassignment after the gate
    before = snapshot(host)
    out = host.deploy(release_v2(), staged=True, automatic="true")
    assert out.returncode != 0 and "RELEASE VERIFIED" not in out.stdout, out.stdout
    assert {c[0] for c in host.containers().values()} == {before[1][n] for n in before[1]}


@pytest.mark.parametrize("interrupted", [False, True])
def test_first_bound_bootstrap_recovers_old_tag_shaped_compose_and_exact_configuration(host, interrupted):
    import gzip
    from app import release_gateway as rg
    v1 = running(host, release_v1(**{"docker-compose.yml": ("x-test-legacy-tags: true\nservices: {}\n", 0o644)}))
    v2 = release_v2()
    ids = host.prepare(v2)
    payload = host.sandboxed(v2).write(host.parent / "inputs-for-bootstrap")
    digest = rg.inputs_digest(gzip.decompress((payload / "src.tgz").read_bytes()))
    host.builds.mkdir(parents=True, exist_ok=True)
    record = {"status": "ready", "inputs_digest": digest, "bootstrap": True, "images": ids, "attempt": 1}
    (host.builds / (digest + ".json")).write_text(json.dumps(record))
    pins = {"inputs_sha256": digest, **ids}
    compose = "x-bound-images: {" + ", ".join(k + ": '" + v + "'" for k, v in pins.items()) + "}\nservices: {}\n"
    v2 = Release(V2, v2.files | {"docker-compose.yml": (compose, 0o644)})
    before = running_binding(host)
    knobs = {"track_configuration": True}
    knobs.update({"crash": "switch:" + V2} if interrupted else {"unhealthy_for": [V2]})
    host.set_knobs(**knobs)
    bootstrapped(host, pending(V2, next_dir(host, V2)))
    result = host.deploy(v2, staged=True, automatic="true",
                         ACK_COMPOSE=__import__("hashlib").sha256(v1.files["docker-compose.yml"][0]).hexdigest()[:12])
    if interrupted:
        assert result.returncode < 0, result.stdout
        host.set_knobs(track_configuration=True)
        result = host.recover(host.backups()[-1])
        assert result.returncode == 0, result.stdout
    else:
        assert result.returncode != 0, result.stdout
    assert host.backups(), result.stdout
    assert host.phase() == "ROLLED_BACK_VERIFIED", result.stdout
    assert running_binding(host) == before
    journal = json.loads((host.backups()[-1] / "recovery-identity.json").read_bytes())
    assert journal["compose_tag"] == "latest"


def _legacy_bootstrap_candidate(host):
    """The legacy host has a tag-shaped applied compose; the candidate supplies only literal pins."""
    import gzip
    from app import release_gateway as rg

    v1 = running(host, release_v1(**{"docker-compose.yml": ("x-test-legacy-tags: true\nservices: {}\n", 0o644)}))
    candidate = release_v2()
    ids = host.prepare(candidate)
    payload = host.sandboxed(candidate).write(host.parent / "inputs-for-bootstrap")
    digest = rg.inputs_digest(gzip.decompress((payload / "src.tgz").read_bytes()))
    record = {"status": "ready", "inputs_digest": digest, "bootstrap": True, "images": ids, "attempt": 1}
    pins = {"inputs_sha256": digest, **ids}
    compose = "x-bound-images: {" + ", ".join(k + ": '" + v + "'" for k, v in pins.items()) + "}\nservices: {}\n"
    return v1, Release(V2, candidate.files | {"docker-compose.yml": (compose, 0o644)}), ids, digest, record


def test_legacy_host_state_applies_the_pins_only_bootstrap_without_rebuilding(host):
    import hashlib

    v1, candidate, ids, digest, record = _legacy_bootstrap_candidate(host)
    host.builds.mkdir(parents=True, exist_ok=True)
    (host.builds / (digest + ".json")).write_text(json.dumps(record))
    with (host.opt / ".env").open("a") as env_file:
        env_file.write("COMPANY_OS_IMAGE_TAG=latest\nCOMPANY_OS_IMAGE_API=company-os-api:latest\n"
                       "COMPANY_OS_IMAGE_WORKER=company-os-worker:latest\n"
                       "COMPANY_OS_IMAGE_REASONER=company-os-reasoner:latest\n"
                       "COMPANY_OS_IMAGE_EGRESS=company-os-egress:latest\n")
    bootstrapped(host, pending(V2, next_dir(host, V2)))

    host.log.write_text("")  # Only apply is forbidden to build; the authorized prepare above must build.
    result = host.deploy(candidate, staged=True, automatic="true",
                         ACK_COMPOSE=hashlib.sha256(v1.files["docker-compose.yml"][0]).hexdigest()[:12])

    assert result.returncode == 0 and "RELEASE VERIFIED: " + V2 in result.stdout, result.stdout
    assert running_binding(host) == ids
    assert not re.search(r"^build ", host.log.read_text(), re.M)


@pytest.mark.parametrize("record_present", [False, True])
def test_legacy_host_refuses_bootstrap_pins_not_matching_the_ready_build_record(host, record_present):
    import hashlib

    v1, candidate, _, digest, record = _legacy_bootstrap_candidate(host)
    record["images"] = dict(record["images"], api="sha256:" + "f" * 64)
    host.builds.mkdir(parents=True, exist_ok=True)
    if record_present:
        (host.builds / (digest + ".json")).write_text(json.dumps(record))
    before = snapshot(host)
    bootstrapped(host, pending(V2, next_dir(host, V2)))

    result = host.deploy(candidate, staged=True, automatic="true",
                         ACK_COMPOSE=hashlib.sha256(v1.files["docker-compose.yml"][0]).hexdigest()[:12])

    assert result.returncode != 0 and "not bound to a root-written ready bootstrap build record" in result.stdout
    assert {name: container[0] for name, container in host.containers().items()} == before[1]
    assert not re.search(r"^build ", host.log.read_text(), re.M)


def test_dotenv_cannot_replace_bound_image_authority(host):
    gateway_host(host)
    v2 = release_v2()
    ids = host.prepare(v2)
    env = host.opt / ".env"
    env.write_text(env.read_text() + "BOUND=no\nBOOTSTRAP=yes\nBOUND_API=sha256:" + "f" * 64 + "\n")
    bootstrapped(host, pending(V2, next_dir(host, V2), pending_images=ids))
    host.log.write_text("")
    result = host.deploy(v2, staged=True, automatic="true")
    assert result.returncode == 0, result.stdout
    assert running_binding(host) == ids
    assert not re.search(r"^build ", host.log.read_text(), re.M)
