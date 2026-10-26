"""The staged deploy transport: root executes nothing it has not copied into its own private directory
and hash-checked. The bootstrap runs for real, as this (non-root) user, against fixture directories;
`--run-root` and `--stage-parent` point it away from /run and /tmp. Nothing touches a host path.
"""
import hashlib
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BOOT = ROOT / "scripts" / "deploy-stage-bootstrap.sh"
DEPLOY = ROOT / "scripts" / "deploy-company-os.sh"
SHA = "a" * 40
ME = os.getuid()
FAKE_DEPLOY = """#!/usr/bin/env bash
echo "RAN as $(id -u) from $0"
echo "SHA=$SHA APPLY=$APPLY DETACH=$DETACH RELEASE_DIR=$RELEASE_DIR ACK_COMPOSE=$ACK_COMPOSE AUTOMATIC=$AUTOMATIC ALLOW_MIGRATION=$ALLOW_MIGRATION"
cat "$RELEASE_DIR/main.sha256"
"""


def h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def host(tmp_path):
    parent = tmp_path / "tmp"
    parent.mkdir()
    parent.chmod(0o1777)                                   # like /tmp: world-writable, sticky
    stage = parent / "company-os-deploy.Ab3dE5gH9j"
    stage.mkdir(mode=0o700)
    files = {"company-os-deploy.sh": FAKE_DEPLOY.encode(), "src.tgz": b"\x1f\x8b fake archive",
             "main.sha256": b"0" * 64 + b"  scripts/companyctl\n"}
    for name, data in files.items():
        (stage / name).write_bytes(data)
    run_root = tmp_path / "run"

    def boot(*, mode="apply", hashes=None, stage_path=None, sudo_uid=str(ME), extra=(), env=None):
        hs = hashes or [h(files["company-os-deploy.sh"]), h(files["src.tgz"]), h(files["main.sha256"])]
        e = ({k: v for k, v in os.environ.items() if k not in ("SUDO_UID", "AUTOMATIC", "ALLOW_MIGRATION")}
             | {"ACK_COMPOSE": "abc123", "AUTOMATIC": "false"} | (env or {}))
        if sudo_uid is not None:
            e["SUDO_UID"] = sudo_uid
        return subprocess.run(["bash", str(BOOT), str(stage_path or stage), SHA, *hs, mode,
                               "--run-root", str(run_root), "--stage-parent", str(parent), *extra],
                              text=True, capture_output=True, env=e)
    boot.stage, boot.parent, boot.run_root, boot.files = stage, parent, run_root, files
    return boot


def copies(host):
    return sorted(host.run_root.iterdir()) if host.run_root.exists() else []


def refused(out, host, why):
    assert out.returncode != 0, out.stdout
    assert "ABORT (staged transport)" in out.stderr and why in out.stderr, out.stderr
    assert "RAN as" not in out.stdout                     # the uploaded script never executed
    assert copies(host) == []                             # no root copy is left behind


def test_a_valid_upload_is_copied_privately_verified_and_only_then_executed(host):
    out = host()
    assert out.returncode == 0, out.stderr
    [d] = copies(host)
    assert d.name.startswith(SHA + ".") and stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(host.run_root.stat().st_mode) == 0o700
    assert f"RAN as {ME} from {d}/company-os-deploy.sh" in out.stdout   # the COPY runs, not the upload
    assert f"RELEASE_DIR={d}" in out.stdout and "APPLY=--apply DETACH=1" in out.stdout
    assert "ACK_COMPOSE=abc123" in out.stdout
    for name, data in host.files.items():
        assert (d / name).read_bytes() == data and stat.S_IMODE((d / name).stat().st_mode) == 0o600


def test_readonly_mode_runs_without_apply_or_detach(host):
    out = host(mode="readonly")
    assert out.returncode == 0 and "APPLY= DETACH= " in out.stdout


@pytest.mark.parametrize("victim", ["company-os-deploy.sh", "src.tgz", "main.sha256"])
def test_a_file_changed_after_hashing_is_refused_before_anything_runs(host, victim):
    hashes = [h(host.files["company-os-deploy.sh"]), h(host.files["src.tgz"]), h(host.files["main.sha256"])]
    (host.stage / victim).write_bytes(b"#!/bin/sh\necho pwned\n")         # replaced after the deployer hashed
    refused(host(hashes=hashes), host, f"{victim} does not match its hash")


def test_an_interrupted_upload_is_refused(host):
    data = host.files["src.tgz"]
    (host.stage / "src.tgz").write_bytes(data[: len(data) // 2])           # truncated
    refused(host(), host, "src.tgz does not match its hash")
    (host.stage / "main.sha256").unlink()                                  # never arrived
    refused(host(), host, "main.sha256 is missing")


def test_a_file_swapped_between_the_checks_and_the_copy_is_caught_by_the_hash(host, tmp_path):
    """`cat` is the copy step: a shim replaces the uploaded script's content just before it is read."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "cat").write_text(f'#!/bin/sh\ncase "$2" in */company-os-deploy.sh) printf "echo pwned\\n" > "$2";; esac\n'
                              f'exec /bin/cat "$@"\n')
    (shim / "cat").chmod(0o755)
    refused(host(env={"PATH": f"{shim}:{os.environ['PATH']}"}), host, "company-os-deploy.sh does not match its hash")


def test_a_symlinked_upload_file_is_refused(host, tmp_path):
    elsewhere = tmp_path / "elsewhere.sh"
    elsewhere.write_bytes(host.files["company-os-deploy.sh"])
    (host.stage / "company-os-deploy.sh").unlink()
    (host.stage / "company-os-deploy.sh").symlink_to(elsewhere)
    refused(host(), host, "company-os-deploy.sh is missing or not a regular file")


def test_a_hardlinked_upload_file_is_refused(host, tmp_path):
    os.link(host.stage / "src.tgz", tmp_path / "second-name")
    refused(host(), host, "src.tgz has 2 hard links")


def test_a_symlinked_staging_directory_is_refused(host):
    link = host.parent / "company-os-deploy.Zz9yY8xX7w"
    link.symlink_to(host.stage)
    refused(host(stage_path=link), host, "is not a real directory")


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777])
def test_a_staging_directory_others_can_enter_or_change_is_refused(host, mode):
    host.stage.chmod(mode)
    refused(host(), host, f"has mode {mode:o}, not 700")


def test_a_staging_directory_owned_by_someone_else_is_refused(host):
    refused(host(sudo_uid=str(ME + 1)), host, f"not the deploying user {ME + 1}")


@pytest.mark.parametrize("name", ["company-os-deploy.short", "company-os-deploy.Ab3dE5gH9j/..", "other.Ab3dE5gH9j",
                                  "company-os-deploy.Ab3dE5gH9!"])
def test_a_staging_path_that_is_not_the_expected_name_is_refused(host, name):
    refused(host(stage_path=str(host.parent / name)), host, "staging directory")


def test_a_staging_directory_outside_the_parent_is_refused(host, tmp_path):
    other = tmp_path / "company-os-deploy.Ab3dE5gH9j"
    other.mkdir(mode=0o700)
    refused(host(stage_path=other), host, "is not in")


def test_a_world_writable_parent_without_the_sticky_bit_is_refused(host):
    host.parent.chmod(0o777)
    refused(host(), host, "without the sticky bit")
    host.parent.chmod(0o1777)


@pytest.mark.parametrize("setup", ["symlink", "wrong-mode", "file"])
def test_an_unsafe_run_root_is_refused(host, tmp_path, setup):
    if setup == "symlink":
        target = tmp_path / "attacker-run"
        target.mkdir(mode=0o700)
        host.run_root.symlink_to(target)
    elif setup == "wrong-mode":
        host.run_root.mkdir(mode=0o755)
    else:
        host.run_root.write_text("x")
    out = host()
    assert out.returncode != 0 and "RAN as" not in out.stdout
    if setup == "symlink":
        assert list((tmp_path / "attacker-run").iterdir()) == []            # nothing written through the link


def test_bad_arguments_and_missing_sudo_are_refused_before_any_check(host):
    assert host(hashes=["x" * 64] * 3).returncode != 0
    assert host(mode="destroy").returncode != 0
    assert host(sudo_uid=None).returncode != 0 and "SUDO_UID" in host(sudo_uid=None).stderr
    assert host(sudo_uid="1000;id").returncode != 0
    assert copies(host) == []


def test_concurrent_deployments_get_separate_private_copies(host):
    outs = []
    threads = [threading.Thread(target=lambda: outs.append(host(mode="readonly"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(o.returncode == 0 for o in outs), [o.stderr for o in outs]
    dirs = {line.split("RELEASE_DIR=")[1].split()[0] for o in outs for line in o.stdout.splitlines() if "RELEASE_DIR=" in line}
    assert len(dirs) == 4 and len(copies(host)) == 4


def test_the_upload_is_never_modified_and_the_bootstrap_never_writes_into_the_stage(host):
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in host.stage.iterdir()}
    assert host().returncode == 0
    assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in host.stage.iterdir()} == before


# --------------------------------------------------------------------------- deploy-company-os.sh wiring

SOURCE = DEPLOY.read_text()
REMOTE = SOURCE[SOURCE.index("<<'REMOTE'\n"):SOURCE.index("\nREMOTE\n")]
LOCAL_TAIL = SOURCE[SOURCE.index("\nREMOTE\n"):]


def test_legacy_transport_stays_the_default_and_is_unchanged():
    staged = LOCAL_TAIL.index('if [ "${COMPANY_OS_DEPLOY_TRANSPORT:-legacy}" = staged ]; then')
    legacy = LOCAL_TAIL.index('scp -q "$TMP/src.tgz" "$TMP/main.sha256" "$TMP/company-os-deploy.sh" "$HOST":/tmp/')
    assert staged < legacy
    staged_block = LOCAL_TAIL[staged:legacy]
    assert "\n  exit 0\nfi\n" in staged_block                                    # staged never falls through to legacy
    assert 'bash /tmp/company-os-deploy.sh"' in LOCAL_TAIL[legacy:]               # CI's current path, untouched


def test_the_staged_branch_validates_the_staging_dir_hashes_first_and_sends_the_bootstrap_on_stdin():
    block = LOCAL_TAIL[LOCAL_TAIL.index("COMPANY_OS_DEPLOY_TRANSPORT"):LOCAL_TAIL.index("\n  exit 0\nfi\n")]
    hashes = block.index("H_SCRIPT=$(sha256")
    stage = block.index("STAGE=$(ssh")
    check = block.index(r'[[ "$STAGE" =~ ^/tmp/company-os-deploy\.[A-Za-z0-9]{10}$ ]]')
    upload = block.index('scp -q "$TMP/src.tgz" "$TMP/main.sha256" "$TMP/company-os-deploy.sh" "$HOST:$STAGE/"')
    assert hashes < stage < check < upload
    assert "umask 077 && mktemp -d /tmp/company-os-deploy.XXXXXXXXXX" in block
    assert "< scripts/deploy-stage-bootstrap.sh" in block and "bash -s --" in block
    assert "/tmp/company-os-deploy.sh" not in block                                # nothing runs from a fixed name


def test_the_root_script_reads_release_files_only_through_rel():
    lines = [l for l in REMOTE.splitlines() if "/tmp/src.tgz" in l or "/tmp/main.sha256" in l]
    assert lines == ['  if [ "$REL" = /tmp ]; then rm -f /tmp/src.tgz /tmp/main.sha256; else rm -rf -- "$REL"; fi']
    assert REMOTE.index("cleanup() {\n" + lines[0]) >= 0   # still the first statement of the EXIT cleanup
    # +1 manifest read: install_cron; +1 src.tgz and +3 manifest reads: the clean build context (extract, verify,
    # compare); +1 manifest read: the executable bit is set on the release's own scripts only
    assert REMOTE.count('"$REL/src.tgz"') == 4 and REMOTE.count('"$REL/main.sha256"') == 11


def rel_block(release_dir):
    start = REMOTE.index("\nREL=/tmp\n") + 1
    block = REMOTE[start:REMOTE.index("\nfi\n", start) + 4]
    env = {k: v for k, v in os.environ.items() if k != "RELEASE_DIR"}
    if release_dir is not None:
        env["RELEASE_DIR"] = release_dir
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + block + 'echo "REL=$REL"'], text=True,
                          capture_output=True, env=env)


@pytest.mark.parametrize("value", ["/tmp", "/run/company-os-deploy/" + "a" * 40, "/run/company-os-deploy/../etc",
                                   "/run/company-os-deploy/" + "a" * 40 + ".abc/../x",
                                   "/run/company-os-deploy/" + "A" * 40 + ".abcdefgh"])
def test_a_release_dir_that_is_not_a_staged_directory_aborts(value):
    out = rel_block(value)
    assert out.returncode != 0 and "ABORT: RELEASE_DIR" in out.stdout


def test_a_well_formed_but_missing_release_dir_aborts_and_unset_means_legacy():
    assert rel_block("/run/company-os-deploy/" + "a" * 40 + ".abcdefgh").returncode != 0   # does not exist here
    out = rel_block(None)
    assert out.returncode == 0 and out.stdout.strip() == "REL=/tmp"


@pytest.mark.parametrize("victim", ["company-os-deploy.sh", "src.tgz", "main.sha256"])
def test_the_hash_is_taken_on_roots_copy_not_on_the_upload(host, tmp_path, victim):
    """The upload stays intact; only what root reads differs (a race during the copy). Hashing the upload
    would pass; hashing root's copy must refuse."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "cat").write_text(f'#!/bin/sh\ncase "$2" in */{victim}) printf "swapped during copy\\n"; exit 0;; esac\n'
                              'exec /bin/cat "$@"\n')
    (shim / "cat").chmod(0o755)
    before = (host.stage / victim).read_bytes()
    refused(host(env={"PATH": f"{shim}:{os.environ['PATH']}"}), host, f"{victim} does not match its hash")
    assert (host.stage / victim).read_bytes() == before


def test_a_staging_directory_owned_by_another_account_is_refused_even_if_its_files_are_not(host, tmp_path):
    """Only root can chown, so `stat` reports a foreign owner for the staging directory alone."""
    shim = tmp_path / "shim"
    shim.mkdir()
    real = "/usr/bin/stat"
    (shim / "stat").write_text(f'#!/bin/sh\nfor last; do :; done\n'
                               f'if [ "$last" = "{host.stage}" ]; then echo "{ME + 1} 700 2"; exit 0; fi\n'
                               f'exec {real} "$@"\n')
    (shim / "stat").chmod(0o755)
    refused(host(env={"PATH": f"{shim}:{os.environ['PATH']}"}), host, f"is owned by uid {ME + 1}, not the deploying user {ME}")



# --------------------------------------------------------------------------- deploy mode across the staged hop

def test_the_deploy_mode_reaches_the_root_copy_unchanged(host):
    out = host(env={"AUTOMATIC": "true", "ALLOW_MIGRATION": "b4f8a2d6c1e9", "ACK_COMPOSE": ""})
    assert out.returncode == 0, out.stderr
    assert "AUTOMATIC=true ALLOW_MIGRATION=b4f8a2d6c1e9" in out.stdout


@pytest.mark.parametrize("env, why", [
    ({"AUTOMATIC": ""}, "deploy mode"), ({"AUTOMATIC": "yes"}, "deploy mode"),
    ({"ALLOW_MIGRATION": "head"}, "ALLOW_MIGRATION"), ({"ALLOW_MIGRATION": "b4f8a2d6c1e9;id"}, "ALLOW_MIGRATION"),
    # Only the release host may combine automatic mode with an acknowledgement (B-7); the staged transport never does.
    ({"AUTOMATIC": "true", "ACK_COMPOSE": "0123456789ab"}, "contradictory"),
    ({"AUTOMATIC": "true", "ACK_COMPOSE": "", "ACK_OVERRIDES": "/x"}, "contradictory"),
])
def test_an_unknown_mode_is_refused_before_the_upload_is_even_looked_at(host, env, why):
    refused(host(env=env), host, why)


def test_the_staged_sudo_line_carries_the_validated_mode():
    line = next(l for l in LOCAL_TAIL.splitlines() if "bash -s --" in l)
    assert "AUTOMATIC=$AUTOMATIC" in line and "ALLOW_MIGRATION=$ALLOW_MIGRATION" in line
