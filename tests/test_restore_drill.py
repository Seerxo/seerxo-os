"""scripts/restore-drill-encrypted.sh with stub ssh/age: passes on good objects, fails loudly."""
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "restore-drill-encrypted.sh"


def _setup(tmp_path, dump=b"PGDMP-fake", with_env=True, with_pg_restore=True, restore_rc=0,
           with_docker=True, db_rc=0, docker_host="", db_counts="tables=1 rows=2", real_postgres=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        name = "opt/company-os/.env" if with_env else "opt/company-os/docker-compose.yml"
        info = tarfile.TarInfo(name)
        info.size = 3
        tar.addfile(info, io.BytesIO(b"x=1"))
    (tmp_path / "config.bin").write_bytes(buf.getvalue())
    (tmp_path / "coolify-db.bin").write_bytes(dump)
    # ssh: the remote command's last word is the target; ciphertext on stdout, R2 key on stderr.
    (bin_dir / "ssh").write_text(f'#!/usr/bin/env bash\nt="${{@: -1}}"; t="${{t##* }}"\n'
                                 f'echo "company-os-host/$t/daily/x.age" >&2\ncat "{tmp_path}/$t.bin"\n')
    # age -d -i KEY -o OUT IN: "decrypts" by copying.
    (bin_dir / "age").write_text('#!/usr/bin/env bash\ncp "$6" "$5"\n')
    # pg_restore: the fake dump lists one table and reads the full archive on request.
    if real_postgres:
        (bin_dir / "pg_restore").symlink_to(shutil.which("pg_restore"))
    elif with_pg_restore:
        (bin_dir / "pg_restore").write_text(
            '#!/usr/bin/env bash\n'
            'if [[ "$*" == *"--list"* ]]; then echo "215; 0 16390 TABLE DATA public t"; exit 0; fi\n'
            'if [[ "$*" == *"--file=/dev/null"* ]]; then cat > /dev/null; exit "${RESTORE_RC:-0}"; fi\n'
            'exit 3\n'
        )
    if with_docker:
        (bin_dir / "docker").write_text(
            '#!/usr/bin/env bash\n'
            'if [ "${1:-}" = --host ]; then shift 2; fi\n'
            'case "$1" in\n'
            'context) echo unix:///test.sock ;;\n'
            'image) echo "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
            'run) printf "%s\\0" "$@" > "$DOCKER_ARGS"\n'
            '  for arg in "$@"; do case "$arg" in --cidfile=*) printf "%064d" 1 > "${arg#--cidfile=}" ;; esac; done\n'
            '  if [[ " $* " != *" --interactive "* ]]; then exec 0</dev/null; fi\n'
            '  if [ "${REAL_POSTGRES:-0}" = 1 ]; then\n'
            '    while [[ "$1" != sha256:* ]]; do shift; done; shift\n'
            '    [ "$1 $2 $3" = "-s KILL 600" ] || exit 3; shift 3\n'
            '    exec sh -xec "${@: -1}" 2>"$DB_TRACE"\n'
            '  fi\n'
            '  cat >/dev/null; echo "$DB_COUNTS"; exit "$DB_RC" ;;\n'
            'rm) printf "%s\\0" "$@" > "$DOCKER_CLEANUP" ;;\n'
            '*) exit 3 ;;\n'
            'esac\n'
        )
        (bin_dir / "docker").chmod(0o755)
    for f in (("ssh", "age", "pg_restore") if with_pg_restore else ("ssh", "age")):
        (bin_dir / f).chmod(0o755)
    (tmp_path / "key").write_text("AGE-SECRET-KEY-TEST")
    # Expose only required utilities so a host-installed pg_restore cannot change the
    # missing-tool test's branch. `tar tzf` also calls gzip through PATH.
    for name in ("bash", "cat", "chmod", "cp", "date", "env", "grep", "gzip", "head", "mktemp", "rm", "tail", "tar", "tr", "wc"):
        executable = shutil.which(name)
        if executable and not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(executable)
    if real_postgres:
        for name in ("initdb", "pg_ctl", "createdb", "psql", "awk", "sh"):
            (bin_dir / name).symlink_to(shutil.which(name))
    env = {**os.environ, "PATH": str(bin_dir), "AGE_KEY": str(tmp_path / "key"), "RESTORE_RC": str(restore_rc),
           "DOCKER_HOST": docker_host, "DOCKER_CONTEXT": "", "DB_RC": str(db_rc), "DB_COUNTS": db_counts,
           "DOCKER_ARGS": str(tmp_path / "docker-args"), "REAL_POSTGRES": str(int(real_postgres)), "PGPORT": "5432",
           "DB_TRACE": str(tmp_path / "db-trace"), "DOCKER_CLEANUP": str(tmp_path / "docker-cleanup")}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def test_good_encrypted_objects_pass(tmp_path):
    out = _setup(tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "OK   coolify-db" in out.stdout and "1 table-data entries" in out.stdout
    assert "OK   config" in out.stdout and "PASSED" in out.stdout
    args = (tmp_path / "docker-args").read_bytes().split(b"\0")
    for option in (b"--interactive", b"--network=none", b"--read-only", b"--cap-drop=ALL", b"--user=postgres", b"--pull=never",
                   b"--security-opt=no-new-privileges", b"--memory=1g", b"--pids-limit=128", b"--cpus=1"):
        assert option in args
    assert not any(a.startswith((b"--mount", b"--volume", b"--publish")) for a in args)
    assert "tables=1 rows=2" in out.stdout
    assert (tmp_path / "docker-cleanup").read_bytes().split(b"\0")[:3] == [b"rm", b"-f", b"0" * 63 + b"1"]


def test_a_wrong_dump_or_missing_env_fails(tmp_path):
    out = _setup(tmp_path, dump=b"not a dump", with_env=False)
    # Break caught: a drill that reports success for objects that would not restore.
    assert out.returncode == 1
    assert "FAIL coolify-db" in out.stdout and "FAIL config" in out.stdout and "FAILED" in out.stdout


def test_a_pg_dump_header_without_pg_restore_is_not_accepted_as_a_restore_check(tmp_path):
    out = _setup(tmp_path, dump=b"PGDMP", with_pg_restore=False)
    assert out.returncode == 1
    assert "pg_restore is required" in out.stdout
    assert "OK   coolify-db" not in out.stdout


def test_a_truncated_dump_with_a_readable_header_and_table_list_fails_full_read(tmp_path):
    out = _setup(tmp_path, dump=b"PGDMP-truncated", restore_rc=1)
    assert out.returncode == 1
    assert "pg_restore --list shows no table data" not in out.stdout
    assert "full archive read failed" in out.stdout
    assert "OK   coolify-db" not in out.stdout


@pytest.mark.parametrize("kwargs", [
    {"db_rc": 1}, {"db_rc": 124}, {"with_docker": False}, {"docker_host": "tcp://remote-daemon:2375"},
    {"db_counts": "not restored"},
])
def test_archive_integrity_alone_cannot_pass_the_database_restore(tmp_path, kwargs):
    out = _setup(tmp_path, **kwargs)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "FAIL coolify-db" in out.stdout and "OK   coolify-db" not in out.stdout
    assert "PASSED" not in out.stdout
    if "db_rc" in kwargs:
        assert (tmp_path / "docker-cleanup").exists()


@pytest.mark.skipif(os.geteuid() == 0 or not all(shutil.which(n) for n in (
    "initdb", "pg_ctl", "pg_dump", "pg_restore", "createdb", "psql")), reason="needs non-root local PostgreSQL tools")
@pytest.mark.parametrize("with_rows", [True, False])
def test_real_postgres_restores_the_dump_and_queries_rows(tmp_path, monkeypatch, with_rows):
    # Execute the exact container SQL procedure natively with synthetic data. Docker isolation is stubbed above.
    with tempfile.TemporaryDirectory(prefix="encrypted-pg-", dir="/tmp") as directory:
        root = Path(directory)
        monkeypatch.setenv("TMPDIR", directory)  # short private Unix socket path, including on macOS
        env = {**os.environ, "PGPORT": "5432"}

        def pg(*args, **kwargs):
            return subprocess.run(args, env=env, check=True, capture_output=True, timeout=30, **kwargs)

        pg("initdb", "-D", str(root / "source"), "-U", "fixture", "-A", "trust", "--no-locale")
        pg("pg_ctl", "-D", str(root / "source"), "-l", str(root / "source.log"), "-w", "start",
           "-o", f"-F -k '{root}' -c listen_addresses=''")
        try:
            sql = 'CREATE TABLE public.sample(n int); CREATE TABLE public."odd"";name"(n int);'
            if with_rows:
                sql += 'INSERT INTO public.sample VALUES (1),(2); INSERT INTO public."odd"";name" VALUES (3);'
            pg("psql", "-X", "-h", directory, "-U", "fixture", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql)
            dump = pg("pg_dump", "-h", directory, "-U", "fixture", "-d", "postgres", "-Fc").stdout
            out = _setup(tmp_path, dump=dump, real_postgres=True)
            trace = (tmp_path / "db-trace").read_text()
            assert out.returncode == (0 if with_rows else 1), out.stdout + out.stderr + trace
            assert "pg_restore -h" in trace and "-f " in trace
            if with_rows:
                assert "isolated restore tables=2 rows=3" in out.stdout
            else:
                assert "isolated database restore or verification failed" in out.stdout
                assert "OK   coolify-db" not in out.stdout
        finally:
            pg("pg_ctl", "-D", str(root / "source"), "-m", "immediate", "-w", "stop")
