"""backup.sh render disk guard. The renders archive is streamed to R2 (scripts/r2.py stream-tar) and never
written to this disk, so the guard only keeps a bounded margin free. The shell functions under test are extracted
from scripts/backup.sh and run for real; only df is stubbed, with the exact output shape it produces."""
import re
import shlex
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup.sh"
GIB = 1024 ** 3


def _fn(name: str) -> str:
    return re.search(rf"^{name}\(\) \{{.*?^\}}", SCRIPT.read_text(), re.S | re.M).group(0)


MARGIN = "RENDER_STREAM_MARGIN=$((1024 ** 3))\n"


def _guard(tmp_path, df_out="", df_rc=0):
    script = (f"fail() {{ echo \"FAIL:$1:$2\"; }}\n"
              f"df() {{ printf '%s\\n' 'Avail'; printf '%s\\n' {shlex.quote(df_out)}; return {df_rc}; }}\n"
              f"BACKUP_LOCAL_DIR={shlex.quote(str(tmp_path))}\nLOG=/dev/null\n{MARGIN}"
              f"{_fn('render_fits')}\n{_fn('require_render_space')}\nrequire_render_space renders")
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return done.returncode, done.stdout


# --- bounded preflight: the archive size no longer matters ------------------------------

def test_the_2026_10_18_situation_now_fits_13_gib_free_for_a_17_gib_archive(tmp_path):
    assert _guard(tmp_path, str(13 * GIB)) == (0, "")


def test_one_gib_free_is_the_exact_boundary(tmp_path):
    assert _guard(tmp_path, str(GIB))[0] == 0
    rc, out = _guard(tmp_path, str(GIB - 1))
    assert rc == 1 and "render archive refused" in out


def test_the_streamed_path_never_writes_a_local_archive():
    body = _fn("dump_renders")
    assert "stream-tar" in body and "BACKUP_LOCAL_DIR" not in body and "tar czf" not in body
    assert "--read-only" in body and "-v \"$vol:/src:ro\"" in body and "--memory" in body


def test_r2_render_archives_are_never_pruned():
    body = _fn("prune_r2")
    assert '[ "$t" = renders ] && {' in body and "continue" in body


# --- fail closed on every measurement ----------------------------------------------------

def test_df_nonzero_exit_refuses(tmp_path):
    rc, out = _guard(tmp_path, str(30 * GIB), df_rc=1)
    assert rc == 1 and "disk measurement failed" in out


def test_malformed_df_output_refuses(tmp_path):
    rc, out = _guard(tmp_path, "not numbers")
    assert rc == 1 and "disk measurement unreadable" in out


def test_an_overlong_df_value_refuses(tmp_path):
    rc, out = _guard(tmp_path, "99999999999999999")
    assert rc == 1 and "disk measurement unreadable" in out


# --- critical backups keep running ------------------------------------------------------

def test_a_refused_render_does_not_stop_the_critical_backups_that_run_before_it():
    source = SCRIPT.read_text()
    main = source[source.index("for t in \"${TARGETS[@]}\"; do"):]
    assert main.index("company-db)") < main.index("renders)")
    assert "fail() {" in source and "FAILED=1" in source[source.index("fail() {"):source.index("fail() {") + 80]


def test_a_refused_render_does_not_stop_a_later_backup_target():
    source = SCRIPT.read_text()
    main = source[source.index('for t in "${TARGETS[@]}"; do'):source.index("\nprune_local\n")]
    script = ("TARGETS=(company-db renders config)\nCLASS=weekly\n"
              "dump_pg() { printf 'ran:%s\\n' \"$1\"; }\n"
              "dump_volumes() { printf 'ran:%s\\n' \"$1\"; }\n"
              "dump_renders() { printf 'ran:renders\\n'; return 1; }\n"
              "dump_config() { printf 'ran:config\\n'; }\n" + main)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["ran:company-db", "ran:renders", "ran:config"]


# --- dump_renders: ok only after the remote re-read --------------------------------------

def _dump(tmp_path, stream_rc=0, stream_out="", verify_rc=0):
    sha = "a" * 64
    out = stream_out or f"{sha} {17 * GIB} 4052"
    script = (f"fail() {{ echo \"FAIL:$1:$2\"; FAILED=1; }}\n"
              "report() { echo \"REPORT:$*\"; }\nexec_log() { :; }\n"
              "require_render_space() { :; }\nr2_configured() { return 0; }\n"
              f"docker() {{ case $1 in volume|inspect) echo company-os-api:x ;; run) echo {shlex.quote(out)}; return {stream_rc} ;; esac; }}\n"
              f"r2() {{ echo \"R2:$*\" >> {shlex.quote(str(tmp_path / 'r2.log'))}; return {verify_rc}; }}\n"
              "LOG=/dev/null\nAPI_CONTAINER=company-api\nR2_ENV=()\nCLASS=weekly\nDAY=2026-10-25\n"
              "STAMP=2026-10-25T013000Z\nFAILED=0\nUPLOADED=()\n"
              f"{_fn('dump_renders')}\ndump_renders youtube-auto-production\n"
              "echo \"failed=$FAILED uploaded=${UPLOADED[*]}\"")
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    calls = tmp_path / "r2.log"
    return done.stdout, calls.read_text() if calls.exists() else ""


def test_a_verified_stream_is_reported_ok(tmp_path):
    out, err = _dump(tmp_path)
    assert f"R2:verify-remote renders/weekly/2026-10-25-renders-2026-10-25T013000Z.tar.gz {'a' * 64} {17 * GIB} 4052" in err
    assert f"REPORT:renders r2:renders/weekly/2026-10-25-renders-2026-10-25T013000Z.tar.gz {17 * GIB} {'a' * 64} 1 1 1" in out
    assert out.strip().endswith("failed=0 uploaded=renders")


def test_a_failed_stream_records_no_success(tmp_path):
    out, _ = _dump(tmp_path, stream_rc=1)
    assert "FAIL:renders:streamed render upload failed" in out and "1 1 1" not in out
    assert out.strip().endswith("failed=1 uploaded=")


def test_a_failed_remote_verification_is_not_ok(tmp_path):
    out, _ = _dump(tmp_path, verify_rc=1)
    assert "R2 round-trip verification failed" in out and " 1 1 1" not in out
    assert out.strip().endswith("failed=1 uploaded=")


def test_malformed_stream_output_is_refused(tmp_path):
    out, _ = _dump(tmp_path, stream_out="garbage")
    assert "malformed output" in out and out.strip().endswith("failed=1 uploaded=")


def test_a_completed_archive_without_its_manifest_is_kept_and_reported_unverified(tmp_path):
    out, calls = _dump(tmp_path, stream_rc=3)
    key = "renders/weekly/2026-10-25-renders-2026-10-25T013000Z.tar.gz"
    assert f"REPORT:renders r2:{key} {17 * GIB} {'a' * 64} 1 0 0 archive completed but its manifest upload failed" in out
    assert "multipart aborted" not in out and "verify-remote" not in calls
    assert out.strip().endswith("failed=1 uploaded=")
