"""The host's companyctl must be the release's companyctl, installed atomically and restorable.

Everything runs against disposable fixtures (a temp "bin" dir, a temp release dir, a temp backup dir);
nothing touches /usr/local/bin or /opt/company-os.
"""
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "scripts" / "install-host-cli.sh"
DEPLOY = ROOT / "scripts" / "deploy-company-os.sh"
RELEASE_CLI = b"#!/usr/bin/env bash\n# release companyctl\necho release\n"
OLD_CLI = b"#!/usr/bin/env bash\n# the older install that predates stage-c status\necho old\n"


@pytest.fixture
def host(tmp_path):
    bin_dir, release, backup = tmp_path / "bin", tmp_path / "opt/scripts", tmp_path / "backup"
    for d in (bin_dir, release, backup):
        d.mkdir(parents=True)
    target = release / "companyctl"
    target.write_bytes(RELEASE_CLI)
    target.chmod(0o755)
    (release / "stage-c-host-facts.py").write_text("# sibling the CLI looks up next to itself\n")
    bin_path = bin_dir / "companyctl"

    def run(mode, *extra, env=None, no_sum=False):
        if mode == "install" and "--expect-sha256" not in extra and not no_sum:
            extra = (*extra, "--expect-sha256", sha(RELEASE_CLI))
        return subprocess.run(
            ["bash", str(HELPER), mode, "--bin", str(bin_path), "--target", str(target),
             "--backup", str(backup), *extra],
            text=True, capture_output=True, env=os.environ | (env or {}))
    run.bin, run.target, run.backup, run.dir, run.release = bin_path, target, backup, bin_dir, release
    return run


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leftovers(host):
    return [p.name for p in host.dir.iterdir() if p.name != "companyctl"]


def test_fresh_host_gets_a_symlink_to_the_release_cli_and_restore_removes_it(host):
    result = host("install", "--expect-sha256", sha(RELEASE_CLI))
    assert result.returncode == 0, result.stderr
    assert host.bin.is_symlink() and host.bin.resolve() == host.target.resolve()
    assert host.bin.read_bytes() == RELEASE_CLI and leftovers(host) == []
    assert host("restore").returncode == 0
    assert not host.bin.exists() and not host.bin.is_symlink()


def test_an_outdated_regular_file_is_replaced_and_restored_byte_for_byte_with_its_mode(host):
    host.bin.write_bytes(OLD_CLI)
    host.bin.chmod(0o755)
    assert host("install").returncode == 0
    assert host.bin.is_symlink() and host.bin.read_bytes() == RELEASE_CLI
    assert (host.backup / "host-cli.prev/file").read_bytes() == OLD_CLI   # kept for the rollback
    assert host("restore").returncode == 0
    assert not host.bin.is_symlink() and host.bin.read_bytes() == OLD_CLI
    assert stat.S_IMODE(host.bin.stat().st_mode) == 0o755
    assert leftovers(host) == []


def test_an_already_current_install_changes_nothing_and_is_idempotent(host):
    assert host("install").returncode == 0
    link_before = os.readlink(host.bin)
    again = host("install")
    assert again.returncode == 0 and "already current" in again.stdout
    assert os.readlink(host.bin) == link_before and leftovers(host) == []
    assert host("restore").returncode == 0           # the recorded first state was "absent"
    assert not host.bin.exists()


def test_an_interrupted_installation_never_leaves_a_partial_executable(host, tmp_path):
    host.bin.write_bytes(OLD_CLI)
    host.bin.chmod(0o755)
    shim = tmp_path / "shim"
    shim.mkdir()
    # the process dies at the final rename onto the CLI path (the backup publish before it succeeds)
    (shim / "mv").write_text('#!/bin/sh\nfor last; do :; done\ncase "$last" in */companyctl) exit 137;; esac\nexec /bin/mv "$@"\n')
    (shim / "mv").chmod(0o755)
    broken = host("install", env={"PATH": f"{shim}:{os.environ['PATH']}"})
    assert broken.returncode != 0
    assert host.bin.read_bytes() == OLD_CLI and not host.bin.is_symlink()     # old CLI, intact
    assert stat.S_IMODE(host.bin.stat().st_mode) == 0o755
    assert any(n.startswith(".companyctl.new.") for n in leftovers(host))     # only a dot-temp remains
    assert host("install").returncode == 0                                    # the retry cleans it up
    assert host.bin.is_symlink() and leftovers(host) == []
    assert (host.backup / "host-cli.prev/file").read_bytes() == OLD_CLI       # first state still the original


def test_a_release_cli_that_cannot_be_trusted_is_refused_and_the_host_is_untouched(host):
    host.bin.write_bytes(OLD_CLI)
    wrong_sum = host("install", "--expect-sha256", "0" * 64)
    assert wrong_sum.returncode != 0 and "checksum" in wrong_sum.stderr
    broken = b"#!/usr/bin/env bash\nif then fi (\n"
    host.target.write_bytes(broken)                                            # does not parse (checksum matches it)
    unparsable = host("install", "--expect-sha256", sha(broken))
    assert unparsable.returncode != 0 and "does not parse" in unparsable.stderr
    host.target.write_bytes(RELEASE_CLI)
    host.target.chmod(0o644)                                                   # not executable: never chmod it for them
    not_exec = host("install")
    assert not_exec.returncode != 0 and "executable" in not_exec.stderr
    assert stat.S_IMODE(host.target.stat().st_mode) == 0o644
    host.target.chmod(0o755)
    host.target.unlink()
    host.target.symlink_to(host.target.parent / "elsewhere")                   # a symlink is not the release file
    assert host("install").returncode != 0
    assert host.bin.read_bytes() == OLD_CLI and not host.bin.is_symlink() and leftovers(host) == []


def test_restore_after_a_failed_release_puts_back_the_previous_symlink_target_too(host, tmp_path):
    previous = tmp_path / "previous-release-companyctl"
    previous.write_bytes(OLD_CLI)
    previous.chmod(0o755)
    host.bin.symlink_to(previous)
    assert host("install").returncode == 0 and host.bin.resolve() == host.target.resolve()
    assert host("restore").returncode == 0
    assert host.bin.is_symlink() and host.bin.resolve() == previous.resolve()


def test_restore_without_a_recorded_state_leaves_the_host_alone(host):
    host.bin.write_bytes(OLD_CLI)
    result = host("restore")
    assert result.returncode == 0 and "nothing recorded" in result.stdout
    assert host.bin.read_bytes() == OLD_CLI


def test_status_reports_current_stale_and_missing(host):
    assert host("status").returncode == 1 and "missing" in host("status").stdout
    host.bin.write_bytes(OLD_CLI)
    assert host("status").returncode == 1 and "stale" in host("status").stdout
    assert host("install").returncode == 0
    assert host("status").returncode == 0 and host("status").stdout.startswith("current")


def test_the_symlink_keeps_the_sibling_script_lookup_working_where_a_copy_would_not(host):
    """companyctl finds stage-c-host-facts.py with dirname(readlink -f $0): the reason this is a symlink."""
    assert host("install").returncode == 0
    resolve_dir = lambda p: subprocess.run(["bash", "-c", 'dirname "$(readlink -f "$1")"', "_", str(p)],
                                           text=True, capture_output=True).stdout.strip()
    assert (Path(resolve_dir(host.bin)) / "stage-c-host-facts.py").exists()
    copy = host.dir / "copied-companyctl"
    copy.write_bytes(RELEASE_CLI)
    assert not (Path(resolve_dir(copy)) / "stage-c-host-facts.py").exists()


def test_no_permission_change_and_nothing_written_outside_bin_dir_and_backup(host, tmp_path):
    target_mode = stat.S_IMODE(host.target.stat().st_mode)
    before = sorted(str(p) for p in tmp_path.rglob("*") if host.dir not in p.parents and host.backup not in p.parents
                    and p not in (host.dir, host.backup))
    assert host("install").returncode == 0
    after = sorted(str(p) for p in tmp_path.rglob("*") if host.dir not in p.parents and host.backup not in p.parents
                   and p not in (host.dir, host.backup))
    assert after == before and stat.S_IMODE(host.target.stat().st_mode) == target_mode
    assert not (host.bin.stat().st_mode & stat.S_IWOTH)   # the target's permissions; a symlink's own mode is 0777 on Linux


def test_the_deploy_script_installs_before_the_switch_and_restores_on_rollback():
    source = DEPLOY.read_text()
    install = source.index('"$HCLI" install --backup "$B"')
    trap = source.index("trap rollback_release ERR")
    switch = source.index("COMPANY_OS_GIT_SHA=$SHA COMPANY_OS_IMAGE_TAG=$SHA up_services bound\n")
    # up_services recreates all four release containers (company-api with the verified pause fence when kept).
    up = source[source.index("up_services() {"):source.index("fence_live() {")]
    for service in ("company-api", "company-worker", "company-reasoner-egress", "company-reasoner"):
        assert up.count(service) >= 2, service   # in both the fence-kept and the base-file branch
    migrate = source.index("== migrate (new image")
    assert migrate < install < trap < switch                       # after the migration gate, before any container switch
    assert "rollback_core" in source[source.index("rollback_release() {"):trap]
    core = source[source.index("rollback_core() {"):source.index("# Recovery after an interrupted deploy")]
    assert '"$B/host-cli.sh" restore' in core
    assert core.index('restore_source "$B"') < core.index('"$B/host-cli.sh" restore') < core.index("up_services")
    assert "--expect-sha256" in source and "scripts/companyctl$" in source   # checked against the release's own checksum list
    assert 'HCLI="$B/host-cli.sh"' in source and 'cp scripts/install-host-cli.sh "$HCLI"' in source  # survives the source restore
    assert os.access(HELPER, os.X_OK)
    for script in (HELPER, DEPLOY):
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


# --------------------------------------------------------------------------- atomic backup manifest

def shim(tmp_path, name, body):
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    (d / name).write_text("#!/bin/sh\n" + body)
    (d / name).chmod(0o755)
    return {"PATH": f"{d}:{os.environ['PATH']}"}


def published(host):
    return (host.backup / "host-cli.prev").exists()


def test_interruption_while_writing_the_backup_publishes_nothing_and_changes_nothing(host, tmp_path):
    host.bin.write_bytes(OLD_CLI)
    host.bin.chmod(0o755)
    dying_cp = shim(tmp_path, "cp", "exit 137\n")                                 # dies copying the old CLI
    broken = host("install", env=dying_cp)
    assert broken.returncode != 0
    assert not published(host) and host.bin.read_bytes() == OLD_CLI and not host.bin.is_symlink()
    assert host("restore").stdout.strip().endswith("left as is")                   # nothing recorded, nothing trusted
    assert host("install").returncode == 0                                         # retry: leftovers cleaned, state complete
    assert published(host) and [p.name for p in host.backup.iterdir()] == ["host-cli.prev"]


def test_interruption_at_the_publish_step_leaves_no_valid_looking_state(host, tmp_path):
    host.bin.write_bytes(OLD_CLI)
    only_publish = shim(tmp_path, "mv", 'case "$*" in *host-cli.prev) exit 137;; esac\nexec /bin/mv "$@"\n')
    broken = host("install", env=only_publish)
    assert broken.returncode != 0 and not published(host)
    assert host.bin.read_bytes() == OLD_CLI and not host.bin.is_symlink()           # the CLI was never switched
    leftover = [p.name for p in host.backup.iterdir()]
    assert leftover and all(n.startswith(".host-cli.prev.tmp.") for n in leftover)  # only a dot-temp, never host-cli.prev
    assert host("install").returncode == 0 and [p.name for p in host.backup.iterdir()] == ["host-cli.prev"]


@pytest.mark.parametrize("damage", ["no-manifest", "no-complete-line", "file-bytes", "unknown-kind", "bad-mode"])
def test_incomplete_or_corrupted_recorded_state_is_rejected_by_install_and_restore(host, damage):
    host.bin.write_bytes(OLD_CLI)
    assert host("install").returncode == 0
    state = host.backup / "host-cli.prev"
    manifest = (state / "manifest").read_text()
    if damage == "no-manifest":
        (state / "manifest").unlink()
    elif damage == "no-complete-line":
        (state / "manifest").write_text(manifest.replace("complete=1\n", ""))
    elif damage == "file-bytes":
        (state / "file").write_bytes(OLD_CLI + b"# tampered\n")
    elif damage == "unknown-kind":
        (state / "manifest").write_text(manifest.replace("kind=file", "kind=banana"))
    else:
        (state / "manifest").write_text(re.sub(r"mode=\d+", "mode=rwx", manifest))
    seen = host.bin.resolve()
    for mode in ("restore", "install"):
        result = host(mode)
        assert result.returncode != 0, (damage, mode, result.stdout)
    assert host.bin.resolve() == seen                                               # neither touched the host CLI


# --------------------------------------------------------------------------- verified rollback

def test_a_restore_that_wrote_the_wrong_bytes_is_detected_not_reported_as_restored(host, tmp_path):
    host.bin.write_bytes(OLD_CLI)
    host.bin.chmod(0o755)
    assert host("install").returncode == 0
    wrong = shim(tmp_path, "cp", 'for last; do :; done\nprintf "garbage" > "$last"\n')   # "copies" the wrong bytes
    result = host("restore", env=wrong)
    assert result.returncode != 0 and "differs from the recorded file" in result.stderr
    assert "restored and verified" not in result.stdout


def test_a_restore_that_lost_the_mode_is_detected(host, tmp_path):
    host.bin.write_bytes(OLD_CLI)
    host.bin.chmod(0o755)
    assert host("install").returncode == 0
    lost_mode = shim(tmp_path, "cp", 'for last; do :; done\n/bin/cp "$2" "$last" && chmod 600 "$last"\n')
    result = host("restore", env=lost_mode)
    assert result.returncode != 0


def test_restore_verifies_the_source_files_it_depends_on_against_their_pre_deploy_checksums(host, tmp_path):
    previous_cli = b"#!/usr/bin/env bash\n# the PRE-deploy release companyctl\n"
    sibling = host.release / "stage-c-host-facts.py"
    old_sibling = b"# pre-deploy sibling\n"
    host.bin.symlink_to(host.target)                                                # prev: a link into the source tree
    sums = tmp_path / "cli-files-before.sha256"
    sums.write_text(f"{sha(previous_cli)}  companyctl\n{sha(old_sibling)}  stage-c-host-facts.py\n")
    root = host.release
    assert host("install", "--expect-sha256", sha(RELEASE_CLI)).returncode == 0     # already current: first state recorded
    # the "tar restore" has NOT happened: the new release bytes are still in the source tree
    stale = host("restore", "--root", str(root), "--sums", str(sums))
    assert stale.returncode != 0 and "differs from its pre-deploy version" in stale.stderr
    host.target.write_bytes(previous_cli)                                           # source restored ...
    sibling.write_bytes(b"# still the NEW sibling\n")                              # ... but not the sibling
    assert host("restore", "--root", str(root), "--sums", str(sums)).returncode != 0
    sibling.write_bytes(old_sibling)                                                # both restored
    ok = host("restore", "--root", str(root), "--sums", str(sums))
    assert ok.returncode == 0 and "restored and verified" in ok.stdout
    assert os.readlink(host.bin) == str(host.target)                                # exact previous symlink identity


def test_a_restored_release_cli_that_does_not_run_is_not_reported_restored(host, tmp_path):
    host.bin.symlink_to(host.target)
    sums = tmp_path / "sums"
    sums.write_text("")
    assert host("install").returncode == 0
    host.target.write_text("#!/usr/bin/env bash\nif then (\n")                    # the restored source is damaged
    result = host("restore", "--root", str(host.release), "--sums", str(sums))
    assert result.returncode != 0 and "not runnable" in result.stderr


def test_malformed_pre_deploy_checksums_fail_the_restore(host, tmp_path):
    host.bin.write_bytes(OLD_CLI)
    assert host("install").returncode == 0
    sums = tmp_path / "sums"
    sums.write_text("not-a-checksum  companyctl\n")
    assert host("restore", "--root", str(host.release), "--sums", str(sums)).returncode != 0


# --------------------------------------------------------------------------- fail-closed release checksum

@pytest.mark.parametrize("value", ["", "abc", "G" * 64, "A" * 64, "a" * 63, "a" * 65])
def test_install_refuses_a_missing_or_malformed_release_checksum_before_touching_anything(host, value):
    host.bin.write_bytes(OLD_CLI)
    result = host("install", "--expect-sha256", value)
    assert result.returncode != 0 and "64-hex" in result.stderr
    assert host.bin.read_bytes() == OLD_CLI and not published(host) and leftovers(host) == []
    absent = host("install", no_sum=True)
    assert absent.returncode != 0 and "64-hex" in absent.stderr and not published(host)


def test_the_deploys_own_manifest_lookup_fails_closed(tmp_path):
    source = DEPLOY.read_text()
    line = next(l for l in source.splitlines() if l.startswith("CLI_SUM=") and "main.sha256" in l)
    good, other = "ab" * 32, "cd" * 32

    def lookup(manifest):
        listing = tmp_path / "main.sha256"
        listing.write_text(manifest)
        script = line.replace('"$REL/main.sha256"', f'"{listing}"') + '\n[[ "$CLI_SUM" =~ ^[0-9a-f]{64}$ ]] && echo "$CLI_SUM" || echo REFUSED\n'
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True).stdout.strip()
    assert lookup(f"{good}  scripts/companyctl\n{other}  scripts/other.sh\n") == good
    assert lookup(f"{other}  scripts/other.sh\n") == "REFUSED"                    # missing manifest entry
    assert lookup(f"{good[:60]}  scripts/companyctl\n") == "REFUSED"                # malformed checksum
    assert lookup(f"{good}  scripts/companyctl-old\n") == "REFUSED"                 # no prefix match


def test_deploy_script_wiring_for_verified_rollback_and_the_preview():
    source = DEPLOY.read_text()
    sums = source.index("cli-files-before.sha256")
    assert sums < source.index("== install source")                                   # captured BEFORE extraction
    install = source.index('"$HCLI" install --backup "$B"')
    gate = source[source.rindex('CLI_SUM=$(sed', 0, install):source.index("\n", install)]
    assert '=~ ^[0-9a-f]{64}$' in gate and '--expect-sha256 "$CLI_SUM"' in gate        # no install without a valid checksum
    install_failure = source[install:source.index("\nfi\n", install)]
    assert 'abort_host_cli_install "could not install the release companyctl' in install_failure
    helper_start = source.index("abort_host_cli_install() {")
    helper = source[helper_start:source.index("\n}\n", helper_start) + 3]
    assert helper.index('restore_source "$B"') < helper.index('"$HCLI" restore --backup "$B"')
    assert helper.index('"$HCLI" restore --backup "$B"') < helper.index("restore_tags")
    assert helper.index("restore_tags") < helper.index("phase ABORTED_RESTORED")
    assert source.index('abort_host_cli_install "could not install the release companyctl') < source.index('echo "== switch containers"')
    core = source[source.index("rollback_core() {"):source.index("# Recovery after an interrupted deploy")]
    assert 'cli_restored=no' in core and '--sums "$B/cli-files-before.sha256"' in core
    verified = core.index("ROLLBACK_RESULT=VERIFIED")
    assert core.rindex('"$cli_restored" = yes', 0, verified) > core.index('"$gate" "$OLD_SHA"')
    rollback = source[source.index("rollback_release() {"):source.index("trap rollback_release ERR")]
    assert "PARTIALLY ROLLED BACK" in rollback and "ROLLBACK FAILED" in rollback     # container behavior preserved
    preview = source[:source.index('if [ "$APPLY" != "--apply" ]; then echo "read-only run')]
    assert "WARNING: main's checksum list has no valid scripts/companyctl entry" in preview and "exit 1" not in preview[preview.index("== host CLI"):]


# --------------------------------------------------------------------------- status: permission audit

def tree_snapshot(root):
    """Every path under root with its type, mode, size, mtime and (for links) target: any write shows up."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        st = p.lstat()
        out[str(p)] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns,
                       os.readlink(p) if p.is_symlink() else None)
    return out


def status_lines(host):
    result = host("status")
    return result.returncode, result.stdout.splitlines()


@pytest.mark.parametrize("mode, group_w, world_w", [(0o755, False, False), (0o775, True, False), (0o777, True, True)])
def test_status_reports_target_permissions_and_warns_on_writable_but_stays_current(host, mode, group_w, world_w):
    assert host("install").returncode == 0
    host.target.chmod(mode)
    code, lines = status_lines(host)
    assert code == 0 and lines[0].startswith("current: ")             # compatible: 775 alone never fails status
    target = next(l for l in lines if l.startswith("target: "))
    assert f"mode={mode:o}" in target and " owner=" in target and " group=" in target
    assert any(l.startswith("WARNING: target is group-writable") for l in lines) is group_w
    assert any(l.startswith("WARNING: target is world-writable") for l in lines) is world_w
    assert stat.S_IMODE(host.target.stat().st_mode) == mode             # reported, never changed


def test_status_also_audits_the_directory_that_holds_the_target(host):
    assert host("install").returncode == 0
    host.target.chmod(0o755)
    host.release.chmod(0o775)
    try:
        code, lines = status_lines(host)
        assert code == 0
        assert any(l.startswith("target directory: ") and "mode=775" in l for l in lines)
        assert any(l.startswith("WARNING: target directory is group-writable") for l in lines)
        assert not any(l.startswith("WARNING: target is") for l in lines)
    finally:
        host.release.chmod(0o755)


def test_status_on_a_stale_symlink_still_exits_1_and_reports_what_it_points_at(host, tmp_path):
    other = tmp_path / "other-companyctl"
    other.write_bytes(OLD_CLI)
    other.chmod(0o777)
    host.bin.symlink_to(other)
    code, lines = status_lines(host)
    assert code == 1 and lines[0].startswith("stale: ")
    assert any(l.startswith("WARNING: target is world-writable") for l in lines)


def gnu_readlink(tmp_path):
    """A `readlink` with GNU `-f` semantics (as on the Linux host): it still prints a path when only the
    last component is missing, where BSD/macOS prints nothing. Other calls go to the real readlink."""
    d = tmp_path / "gnu"
    d.mkdir(exist_ok=True)
    (d / "readlink").write_text(f"""#!/bin/sh
if [ "$1" = -f ]; then
  [ "$2" = -- ] && shift
  exec "{sys.executable}" -c 'import os,sys; p=os.path.realpath(sys.argv[1]); sys.exit(1) if not os.path.isdir(os.path.dirname(p)) else print(p)' "$2"
fi
exec /usr/bin/readlink "$@"
""")
    (d / "readlink").chmod(0o755)
    return {"PATH": f"{d}:{os.environ['PATH']}"}


@pytest.mark.parametrize("readlink", ["platform", "gnu"])
def test_status_with_an_absent_target_reports_it_and_never_creates_it(host, tmp_path, readlink):
    """A dangling link is never "current", whichever readlink -f semantics the host has (CI found the GNU case)."""
    env = gnu_readlink(tmp_path) if readlink == "gnu" else None
    assert host("install").returncode == 0
    host.target.unlink()                                                  # the link now dangles
    result = host("status", env=env)
    lines = result.stdout.splitlines()
    assert result.returncode == 1 and lines[0].startswith("stale: "), result.stdout
    assert "target: missing (the link resolves to nothing)" in lines
    assert not host.target.exists()
    host.bin.unlink()
    result = host("status", env=env)
    assert result.returncode == 1 and result.stdout.splitlines() == [f"missing: {host.bin}"]

def test_status_is_strictly_read_only(host, tmp_path):
    host.bin.write_bytes(OLD_CLI)
    for setup in ("stale-file", "current", "dangling"):
        if setup == "current":
            host.bin.unlink(); assert host("install").returncode == 0; host.target.chmod(0o777)
        if setup == "dangling":
            host.target.unlink()
        before = tree_snapshot(tmp_path)
        host("status")
        assert tree_snapshot(tmp_path) == before, setup                 # no file, mode, link or mtime changed
