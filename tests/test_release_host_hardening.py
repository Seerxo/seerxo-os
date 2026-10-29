"""Release hardening on the host side: root never executes a script from a fixed /tmp path, and the deploy
proves the extracted release files have the archive's (non-group/world-writable) modes.

All tests run on disposable fixtures; nothing touches a host path.
"""
import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "scripts" / "deploy-company-os.sh"
VERIFY = ROOT / "scripts" / "verify-release-permissions.py"
SOURCE = DEPLOY.read_text()
REMOTE = SOURCE[SOURCE.index("<<'REMOTE'\n"):SOURCE.index("\nREMOTE\n")]


@pytest.mark.parametrize("exit_line, changed_pid, expected", [
    ("EXIT 0", False, 0),
    ("EXIT 7", False, 7),
    ("EXIT 0", True, 3),
])
def test_follow_waits_after_ssh_disconnect_and_checks_the_same_deploy(tmp_path, exit_line, changed_pid, expected):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "ssh").write_text("""#!/usr/bin/env bash
case "$*" in
  *'tail -n +1'*) exit 255 ;;
  *'tail -n 1'*)
    if [ ! -f "$FOLLOW_POLLED" ]; then
      touch "$FOLLOW_POLLED"
      printf '42\\nRUNNING\\n'
    else
      printf '%s\\n%s\\n' "$FOLLOW_FINAL_PID" "$FOLLOW_EXIT_LINE"
    fi ;;
  *) printf '42\\n' ;;
esac
""")
    (fake_bin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    for stub in fake_bin.iterdir():
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "FOLLOW_POLLED": str(tmp_path / "polled"), "FOLLOW_FINAL_PID": "43" if changed_pid else "42",
           "FOLLOW_EXIT_LINE": exit_line}
    out = subprocess.run(["bash", str(DEPLOY), "--follow"], capture_output=True, text=True, env=env, timeout=10)
    assert out.returncode == expected, out.stdout + out.stderr
    assert (tmp_path / "polled").exists()
    assert "deploying origin/main" not in out.stdout
    if changed_pid:
        assert "deploy run changed" in out.stderr


# --------------------------------------------------------------------------- no root exec from fixed /tmp

def test_root_never_runs_a_helper_copied_to_a_fixed_tmp_path():
    """A local user who pre-creates /tmp/<name> keeps owning it after root's `cp` writes into it (cp opens an
    existing file without O_CREAT, so fs.protected_regular does not stop it), and can change it before root
    executes it. The helpers root runs must live in the root-created deploy backup directory instead."""
    for name in ("company-os-release-health-gate.sh", "company-os-host-cli.sh"):
        assert f"/tmp/{name}" not in REMOTE, name
    assert 'cp scripts/deploy-health-gate.sh "$GATE"' in REMOTE and 'cp scripts/install-host-cli.sh "$HCLI"' in REMOTE
    assert 'GATE="$B/release-health-gate.sh"' in REMOTE and 'HCLI="$B/host-cli.sh"' in REMOTE
    # B is created before either copy, and the copies happen before anything executes them
    mk = REMOTE.index('B=$(new_backup_dir /opt/company-os "$S" "$TRUSTED_UID")')
    assert mk < REMOTE.index('GATE="$B/') < REMOTE.index('cp scripts/deploy-health-gate.sh "$GATE"') < REMOTE.index('"$GATE" "$SHA"')
    assert mk < REMOTE.index('HCLI="$B/') < REMOTE.index('cp scripts/install-host-cli.sh "$HCLI"') < REMOTE.index('"$HCLI" install')


def test_the_helper_copies_survive_restoring_the_old_source():
    """Rollback re-extracts files-before.tgz into /opt/company-os: B (backups/...) must not be release content."""
    paths = SOURCE[SOURCE.index('PATHS="') + 7:].split('"', 1)[0].split()
    assert 'dir="backups/deploy-$stamp"' in REMOTE
    assert not any(p == "backups" or p.startswith("backups/") for p in paths)
    core = REMOTE[REMOTE.index("rollback_core() {"):REMOTE.index("# Recovery after an interrupted deploy")]
    assert '"$B/host-cli.sh" restore' in core and '"$gate" "$OLD_SHA"' in core and 'gate=$B/release-health-gate.sh' in core
    assert core.index('restore_source "$B"') < core.index('"$B/host-cli.sh" restore')
    assert "rollback_core" in REMOTE[REMOTE.index("rollback_release() {"):REMOTE.index("trap rollback_release ERR")]


# --------------------------------------------------------------------------- verify-release-permissions

def make_archive(path: Path, entries):
    """entries: (name, kind, mode, payload) with kind in file|dir|symlink."""
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, mode, payload in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, payload
                tar.addfile(info)
            else:
                data = payload.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


RELEASE = [("scripts", "dir", 0o755, None), ("scripts/companyctl", "file", 0o755, "#!/bin/sh\n"),
           ("app", "dir", 0o755, None), ("app/main.py", "file", 0o644, "x = 1\n"),
           ("app/link.py", "symlink", 0o777, "main.py")]


@pytest.fixture
def release(tmp_path):
    archive = tmp_path / "src.tgz"
    make_archive(archive, RELEASE)
    root = tmp_path / "opt"
    root.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(root, filter="tar")   # keeps the archive's modes, like root's GNU tar
    for name, kind, mode, _ in RELEASE:
        if kind != "symlink":
            (root / name).chmod(mode)

    def run(*extra):
        return subprocess.run([sys.executable, str(VERIFY), "--root", str(root), "--archive", str(archive), *extra],
                              text=True, capture_output=True)
    run.root, run.archive = root, archive
    return run


def snapshot(base: Path):
    return {str(p): (p.lstat().st_mode, p.lstat().st_size, p.lstat().st_mtime_ns) for p in sorted(base.rglob("*"))}


def test_a_matching_secure_release_passes(release):
    out = release()
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().splitlines()[-1].startswith("RELEASE PERMISSIONS: OK")


@pytest.mark.parametrize("name, mode, finding", [
    ("scripts/companyctl", 0o775, "group-writable"),
    ("app/main.py", 0o646, "world-writable"),
    ("app", 0o775, "group-writable"),
    ("scripts/companyctl", 0o644, "mode 644, archive says 755"),     # lost its executable bit
])
def test_unsafe_or_mismatched_modes_are_reported(release, name, mode, finding):
    (release.root / name).chmod(mode)
    out = release()
    assert out.returncode == 1
    assert any(name in l and finding in l for l in out.stdout.splitlines()), out.stdout
    assert "RELEASE PERMISSIONS: FAIL" in out.stdout


def test_a_missing_file_and_a_retargeted_or_dangling_symlink_are_reported(release):
    (release.root / "app/main.py").unlink()                           # also leaves app/link.py dangling
    out = release()
    assert out.returncode == 1 and "app/main.py: missing" in out.stdout
    (release.root / "app/link.py").unlink()
    (release.root / "app/link.py").symlink_to("/etc/passwd")
    out = release()
    assert "app/link.py: symlink to /etc/passwd, archive says main.py" in out.stdout


def test_a_regular_file_where_the_archive_has_a_symlink_is_reported(release):
    (release.root / "app/link.py").unlink()
    (release.root / "app/link.py").write_text("not a link\n")
    out = release()
    assert out.returncode == 1 and "app/link.py: not a symlink" in out.stdout


def test_an_archive_entry_that_is_itself_writable_by_others_is_reported(tmp_path, release):
    bad = tmp_path / "bad.tgz"
    make_archive(bad, [*RELEASE[:3], ("app/main.py", "file", 0o664, "x = 1\n"), RELEASE[4]])
    (release.root / "app/main.py").chmod(0o664)                        # host matches the (unsafe) archive
    out = subprocess.run([sys.executable, str(VERIFY), "--root", str(release.root), "--archive", str(bad)],
                         text=True, capture_output=True)
    assert out.returncode == 1 and "app/main.py" in out.stdout and "group-writable" in out.stdout


@pytest.mark.parametrize("content", [b"", b"not a tarball", b"\x1f\x8b\x08\x00garbage"])
def test_a_corrupted_or_unreadable_archive_is_an_error_not_a_pass(tmp_path, content):
    archive = tmp_path / "src.tgz"
    archive.write_bytes(content)
    out = subprocess.run([sys.executable, str(VERIFY), "--root", str(tmp_path), "--archive", str(archive)],
                         text=True, capture_output=True)
    assert out.returncode == 2 and "OK" not in out.stdout


def test_unsafe_archive_paths_are_refused_not_followed(tmp_path):
    archive = tmp_path / "src.tgz"
    make_archive(archive, [("../escape", "file", 0o644, "x"), ("/abs", "file", 0o644, "x")])
    out = subprocess.run([sys.executable, str(VERIFY), "--root", str(tmp_path / "opt"), "--archive", str(archive)],
                         text=True, capture_output=True)
    assert out.returncode == 2 and "unsafe path" in out.stderr


def test_the_verifier_is_strictly_read_only(release, tmp_path):
    (release.root / "scripts/companyctl").chmod(0o775)                  # a finding, so every branch runs
    before = snapshot(tmp_path)
    release()
    assert snapshot(tmp_path) == before


def test_the_real_release_archive_verifies_clean_when_extracted(tmp_path):
    """End to end on this repository: the deploy's own packing line, extracted, then verified."""
    block = SOURCE[SOURCE.index("\nPATHS=") + 1:SOURCE.index('> "$TMP/src.tgz"') + len('> "$TMP/src.tgz"')]
    for line in ("TMP=$(mktemp -d)\n", "trap 'rm -rf \"$TMP\"' EXIT\n"):
        block = block.replace(line, "", 1)
    subprocess.run(["bash", "-c", f'set -euo pipefail\ncd "$1"\nSHA=$(git rev-parse HEAD)\nTMP="$2"\n{block}\n', "_",
                    str(ROOT), str(tmp_path)], check=True, capture_output=True)
    root = tmp_path / "opt"
    root.mkdir()
    with tarfile.open(tmp_path / "src.tgz") as tar:
        tar.extractall(root, filter="tar")
        for m in tar.getmembers():
            if not m.issym():
                (root / m.name).chmod(m.mode)
    out = subprocess.run([sys.executable, str(VERIFY), "--root", str(root), "--archive", str(tmp_path / "src.tgz")],
                         text=True, capture_output=True)
    assert out.returncode == 0, out.stdout[-2000:]


# --------------------------------------------------------------------------- deploy wiring

def test_a_valid_but_empty_archive_is_an_error_not_a_vacuous_pass(tmp_path):
    archive = tmp_path / "src.tgz"
    make_archive(archive, [])
    out = subprocess.run([sys.executable, str(VERIFY), "--root", str(tmp_path), "--archive", str(archive)],
                         text=True, capture_output=True)
    assert out.returncode == 2 and "no entries" in out.stderr and "OK" not in out.stdout


def test_the_archive_can_be_streamed_on_stdin_so_host_checks_write_nothing(release):
    data = release.archive.read_bytes()
    ok = subprocess.run([sys.executable, str(VERIFY), "--root", str(release.root), "--archive", "-"],
                        input=data, capture_output=True)
    assert ok.returncode == 0 and b"RELEASE PERMISSIONS: OK" in ok.stdout
    (release.root / "app/main.py").chmod(0o664)
    bad = subprocess.run([sys.executable, str(VERIFY), "--root", str(release.root), "--archive", "-"],
                         input=data, capture_output=True)
    assert bad.returncode == 1 and b"group-writable" in bad.stdout
    garbage = subprocess.run([sys.executable, str(VERIFY), "--root", str(release.root), "--archive", "-"],
                             input=b"not a tar", capture_output=True)
    assert garbage.returncode == 2


# --------------------------------------------------------------------------- trusted backup directory (Task 1)

def remote_function(name: str) -> str:
    """The exact text of a shell function defined in the deploy's root-run part."""
    start = REMOTE.index(f"\n{name}() {{\n") + 1
    return REMOTE[start:REMOTE.index("\n}\n", start) + 3]


def run_functions(script: str, *args, env=None):
    body = remote_function("trusted_dir") + remote_function("new_backup_dir") + script
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + body, "_", *map(str, args)],
                          text=True, capture_output=True, env=os.environ | (env or {}))


ME = os.getuid()


@pytest.fixture
def opt(tmp_path):
    """A stand-in for /opt/company-os. trusted_dir walks the REAL ancestors, so the fixture must live under a
    chain nobody else can write: pytest's tmp_path is under /tmp (1777) on Linux CI, which trusted_dir rightly
    refuses. Use the first candidate whose chain trusted_dir itself accepts, else skip."""
    import shutil
    import tempfile
    for candidate in (Path.home(), ROOT, Path(tempfile.gettempdir()), tmp_path):
        try:
            work = Path(tempfile.mkdtemp(prefix=".release-trust-", dir=candidate)).resolve()
        except OSError:
            continue
        if run_functions('trusted_dir "$1" "$2"', work, ME).returncode == 0:
            break
        shutil.rmtree(work, ignore_errors=True)
    else:
        pytest.skip("no candidate directory has an ancestor chain writable only by root and this user")
    base = work / "company-os"
    base.mkdir()
    base.chmod(0o755)
    try:
        yield base
    finally:
        for p in (base, base / "backups"):
            if p.exists() and not p.is_symlink():
                p.chmod(0o755)                                          # tests may leave a parent unwritable
        shutil.rmtree(work, ignore_errors=True)


def test_trusted_dir_accepts_a_clean_owned_chain(opt):
    out = run_functions('trusted_dir "$1" "$2"', opt, ME)
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o1777])
def test_trusted_dir_rejects_a_group_or_world_writable_directory_anywhere_on_the_path(opt, mode):
    (opt / "backups").mkdir()
    for target in (opt, opt / "backups"):
        target.chmod(mode)
        out = run_functions('trusted_dir "$1/backups" "$2"', opt, ME)
        assert out.returncode != 0 and "untrusted" in out.stderr, (oct(mode), target)
        target.chmod(0o755)


def test_trusted_dir_rejects_a_symlink_in_the_chain(opt, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    real.chmod(0o755)
    (opt / "backups").symlink_to(real)
    out = run_functions('trusted_dir "$1/backups" "$2"', opt, ME)
    assert out.returncode != 0 and "not a real directory" in out.stderr


def test_trusted_dir_rejects_a_directory_owned_by_someone_else(opt):
    """Ownership: as a non-root test the only foreign owner available is root, so trust only ME+1."""
    out = run_functions('trusted_dir "$1" "$2"', opt, ME + 1)
    if ME == 0:
        pytest.skip("running as root: every fixture is root-owned")
    assert out.returncode != 0 and "owned by uid" in out.stderr


@pytest.mark.parametrize("suffix", ["/backups/..", "/backups/.", "/./backups", "//backups"])
def test_trusted_dir_rejects_dot_and_empty_components_even_when_they_would_resolve_to_a_trusted_dir(opt, suffix):
    (opt / "backups").mkdir(mode=0o755)
    assert run_functions('trusted_dir "$1" "$2"', f"{opt}/backups", ME).returncode == 0
    out = run_functions('trusted_dir "$1" "$2"', f"{opt}{suffix}", ME)
    assert out.returncode != 0 and "component" in out.stderr


def test_trusted_dir_rejects_a_relative_path():
    out = run_functions('trusted_dir "$1" "$2"', "relative/dir", ME)
    assert out.returncode != 0 and "not absolute" in out.stderr


def test_new_backup_dir_creates_a_fresh_private_directory(opt):
    out = run_functions('new_backup_dir "$1" STAMP "$2"', opt, ME)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "backups/deploy-STAMP"
    made = opt / "backups" / "deploy-STAMP"
    assert made.is_dir() and not made.is_symlink()
    assert stat.S_IMODE(made.stat().st_mode) == 0o700
    assert stat.S_IMODE((opt / "backups").stat().st_mode) == 0o755


@pytest.mark.parametrize("collision", ["dir", "file", "symlink-to-dir", "dangling-symlink"])
def test_a_pre_created_deploy_path_is_refused_never_reused(opt, tmp_path, collision):
    (opt / "backups").mkdir(mode=0o755)
    target = opt / "backups" / "deploy-STAMP"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "host-cli.sh").write_text("#!/bin/sh\necho owned\n")
    if collision == "dir":
        target.mkdir(mode=0o700)
    elif collision == "file":
        target.write_text("x")
    elif collision == "symlink-to-dir":
        target.symlink_to(planted)
    else:
        target.symlink_to(tmp_path / "nowhere")
    before = sorted(p.name for p in planted.iterdir())
    out = run_functions('new_backup_dir "$1" STAMP "$2"', opt, ME)
    assert out.returncode != 0 and out.stdout == ""
    assert sorted(p.name for p in planted.iterdir()) == before          # nothing written through the link


def test_new_backup_dir_refuses_an_untrusted_parent_without_creating_anything(opt):
    (opt / "backups").mkdir(mode=0o755)
    (opt / "backups").chmod(0o777)
    out = run_functions('new_backup_dir "$1" STAMP "$2"', opt, ME)
    assert out.returncode != 0 and out.stdout == ""
    assert list((opt / "backups").iterdir()) == []
    (opt / "backups").chmod(0o755)


def test_new_backup_dir_never_repairs_permissions(opt):
    (opt / "backups").mkdir(mode=0o755)
    opt.chmod(0o775)
    out = run_functions('new_backup_dir "$1" STAMP "$2"', opt, ME)
    assert out.returncode != 0
    assert stat.S_IMODE(opt.stat().st_mode) == 0o775                     # reported, not chmodded
    opt.chmod(0o755)


def test_a_symlinked_backups_directory_is_refused(opt, tmp_path):
    real = tmp_path / "real-backups"
    real.mkdir(mode=0o755)
    (opt / "backups").symlink_to(real)
    out = run_functions('new_backup_dir "$1" STAMP "$2"', opt, ME)
    assert out.returncode != 0 and list(real.iterdir()) == []


def test_the_deploy_creates_b_through_the_trusted_path_before_any_mutation_and_aborts_otherwise():
    create = REMOTE.index('B=$(new_backup_dir /opt/company-os "$S" "$TRUSTED_UID")')
    line = REMOTE[create:REMOTE.index("\n", REMOTE.index("\n", create) + 1)]
    assert "ABORT" in line and "exit 1" in line                         # untrusted: stop, nothing changed yet
    assert 'mkdir -p "$B"' not in REMOTE
    assert create < REMOTE.index('tar -czf "$B/files-before.tgz"') < REMOTE.index("== install source")
    assert create < REMOTE.index('cp scripts/deploy-health-gate.sh "$GATE"')
    assert create < REMOTE.index('cp scripts/install-host-cli.sh "$HCLI"')
    # the read-only preview reports the same trust check, without creating anything
    preview = REMOTE[REMOTE.index('echo "== deploy path trust"'):REMOTE.index('if [ "$APPLY" != "--apply" ]; then echo "read-only run')]
    assert 'trusted_dir /opt/company-os "$TRUSTED_UID"' in preview and "new_backup_dir" not in preview


# --------------------------------------------------------------------------- verification states and gate (Task 2)

def test_an_unexpected_verifier_crash_is_could_not_verify_not_a_finding(release):
    """Python exits 1 on an uncaught exception, which would look like 'found unsafe permissions'."""
    code = ("import runpy, sys, tarfile\n"
            "def boom(*a, **k): raise RuntimeError('unexpected')\n"
            "tarfile.open = boom\n"
            f"sys.argv = ['v', '--root', {str(release.root)!r}, '--archive', {str(release.archive)!r}]\n"
            f"runpy.run_path({str(VERIFY)!r}, run_name='__main__')\n")
    out = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
    assert out.returncode == 2 and "cannot verify" in out.stderr and "OK" not in out.stdout


def gate(release, enforce: str, *, python=None, archive=None):
    """Run the deploy's own check_release_permissions with its paths pointed at the fixture."""
    fn = remote_function("check_release_permissions")
    fn = fn.replace("/opt/company-os", str(release.root)).replace('"$REL/src.tgz"', f'"{archive or release.archive}"')
    fn = fn.replace("python3 scripts/verify-release-permissions.py", f"{python or sys.executable} {VERIFY}")
    script = f"set -euo pipefail\nENFORCE_RELEASE_PERMISSIONS={enforce}\n{fn}\ncheck_release_permissions\n"
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True)


@pytest.mark.parametrize("enforce", ["false", "true"])
def test_gate_passes_a_clean_release_in_both_modes(release, enforce):
    out = gate(release, enforce)
    assert out.returncode == 0 and "RELEASE PERMISSIONS: OK" in out.stdout


@pytest.mark.parametrize("state", ["finding", "invalid-archive", "missing-path", "no-python"])
def test_gate_names_each_state_and_never_reports_it_verified(release, tmp_path, state):
    archive, python = None, None
    if state == "finding":
        (release.root / "scripts/companyctl").chmod(0o775)
    elif state == "invalid-archive":
        archive = tmp_path / "broken.tgz"
        archive.write_bytes(b"garbage")
    elif state == "missing-path":
        (release.root / "app/main.py").unlink()
    else:
        python = tmp_path / "no-such-python"
    report = gate(release, "false", python=python, archive=archive)
    text = report.stdout + report.stderr
    assert "RELEASE PERMISSIONS: OK" not in report.stdout
    expected = "UNVERIFIED" if state in ("invalid-archive", "no-python") else "RELEASE PERMISSIONS: FAIL"
    assert expected in text, text
    assert report.returncode == 0 and "report-only" in text          # today: never blocks
    enforced = gate(release, "true", python=python, archive=archive)
    assert enforced.returncode != 0 and expected in enforced.stdout + enforced.stderr


def test_enforcement_is_off_in_source_and_its_abort_restores_files_before_any_switch():
    assert "\nENFORCE_RELEASE_PERMISSIONS=false\n" in REMOTE                 # flipping it is a reviewed change
    call = REMOTE.index("if ! check_release_permissions; then")
    block = REMOTE[call:REMOTE.index("\nfi\n", call)]
    assert 'abort_before_migration "release file permissions are not verified"' in block
    helper_start = REMOTE.index("abort_before_migration() {")
    helper = REMOTE[helper_start:REMOTE.index("\n}\n", helper_start) + 3]
    assert helper.index('restore_source "$B"') < helper.index("restore_tags")
    assert helper.index("restore_tags") < helper.index("phase ABORTED_RESTORED")
    installed = REMOTE.index('tar --no-same-owner -xzf "$REL/src.tgz" -C /opt/company-os')
    bound = REMOTE.index('if [ "$BOUND" = yes ] || [ "$BOOTSTRAP" = yes ]; then', call)
    legacy_build = REMOTE.index("docker build -q --target", bound)
    assert installed < call < bound < legacy_build
    assert "prebind_images" in REMOTE[bound:legacy_build]
    assert call < REMOTE.index("trap rollback_release ERR")
    assert "release file permissions differ from the archive" not in REMOTE   # the old one-size-fits-all message


@pytest.mark.parametrize("exit_code, output", [(1, "Traceback: interpreter broke"), (0, ""), (0, "OK-ish")])
def test_an_exit_code_without_the_verifiers_own_verdict_is_unverified(release, tmp_path, exit_code, output):
    fake = tmp_path / "fake-python"
    fake.write_text(f"#!/bin/sh\necho '{output}'\nexit {exit_code}\n")
    fake.chmod(0o755)
    out = gate(release, "false", python=fake)
    assert "UNVERIFIED" in out.stdout and "RELEASE PERMISSIONS: OK" not in out.stdout
    assert "RELEASE PERMISSIONS: FAIL:" not in out.stdout
    assert gate(release, "true", python=fake).returncode != 0



def trusted_uid(sudo_uid):
    """The deploy's own TRUSTED_UID derivation, run with a given SUDO_UID (None = unset)."""
    start = REMOTE.index("\nTRUSTED_UID=0\n") + 1
    block = REMOTE[start:REMOTE.index("\nesac\n", start) + 6]
    env = {k: v for k, v in os.environ.items() if k != "SUDO_UID"}
    if sudo_uid is not None:
        env["SUDO_UID"] = sudo_uid
    out = subprocess.run(["bash", "-c", block + 'echo "$TRUSTED_UID"'], text=True, capture_output=True, env=env)
    return out.stdout.strip()


@pytest.mark.parametrize("sudo_uid, expected", [
    (None, "0"), ("", "0"), ("1000", "1000"), ("0", "0"),
    ("1000; rm -rf /", "0"), ("-1", "0"), ("1e3", "0"), (" 1000", "0"),
])
def test_only_root_and_the_numeric_sudo_invoker_are_trusted_owners(sudo_uid, expected):
    """The host's /opt/company-os is owned by the deploy user (semih, uid 1000), who already holds sudo:
    trusting that uid adds nothing an attacker in that account could not do via sudo. Anything that is
    not a plain number falls back to root only."""
    assert trusted_uid(sudo_uid) == expected


def test_a_directory_owned_by_the_sudo_invoker_is_trusted_but_not_by_anyone_else(opt):
    assert run_functions('trusted_dir "$1" "$2"', opt, ME).returncode == 0
    if ME != 0:
        assert run_functions('trusted_dir "$1" "$2"', opt, 0).returncode != 0   # root-only: my dir is refused
