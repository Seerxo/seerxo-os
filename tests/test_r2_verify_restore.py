"""scripts/r2.py verify-restore: a streamed renders archive is proven restorable only if every member is a
regular file at a safe path, listed once in the manifest with the same size and SHA-256, every manifest entry
is present, and the object matches the manifest's archive hash. No network: an in-memory fake S3."""
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import r2
from tests.test_r2_stream_tar import FakeS3, src  # noqa: F401 (fixture)

KEY = "renders/weekly/2026-10-25-renders.tar.gz"


def archive(members, manifest_files=None, archive_override=None):
    """members: (name, bytes | None for a symlink). The manifest lists the members (or manifest_files)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type, info.linkname = tarfile.SYMTYPE, "../../etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    files = manifest_files if manifest_files is not None else [
        {"path": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in members if d is not None]
    s3 = FakeS3()
    s3.objects[KEY] = archive_override if archive_override is not None else blob
    s3.objects[KEY + ".manifest.json"] = json.dumps(
        {"archive_sha256": hashlib.sha256(blob).hexdigest(), "archive_size": len(blob), "files": files}).encode()
    return s3


GOOD = [("a/b/v1.mp4", os.urandom(5000)), ("top.bin", b"x" * 300)]


def test_a_streamed_archive_restores_file_for_file(src):  # noqa: F811
    s3 = FakeS3()
    made = r2.stream_tar(s3, "b", KEY, str(src), part_size=64 * 1024)
    out = r2.verify_restore(s3, "b", KEY)
    assert out["ok"] and out["files"] == made["files"] and not (out["mismatches"] or out["missing"] or out["extra"])
    assert out["bytes"] == sum(p.stat().st_size for p in src.rglob("*") if p.is_file())


def test_a_hand_built_good_archive_passes():
    assert r2.verify_restore(archive(GOOD), "b", KEY)["ok"]


@pytest.mark.parametrize("case", ["corrupt_member", "missing", "extra", "symlink", "traversal", "absolute",
                                  "duplicate", "object_mismatch", "truncated"])
def test_every_restore_defect_fails(case):
    files = [{"path": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in GOOD]
    if case == "corrupt_member":       # same name and size, different bytes than the manifest recorded
        s3 = archive([GOOD[0], ("top.bin", b"y" * 300)], manifest_files=files)
    elif case == "missing":
        s3 = archive(GOOD[:1], manifest_files=files)
    elif case == "extra":
        s3 = archive(GOOD + [("new.bin", b"z")], manifest_files=files)
    elif case == "symlink":            # even when the manifest names it as a file, a link is never restored
        s3 = archive(GOOD + [("link", None)],
                     manifest_files=files + [{"path": "link", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}])
    elif case == "traversal":
        s3 = archive(GOOD + [("a/../../evil", b"e")], manifest_files=files)
    elif case == "absolute":
        s3 = archive(GOOD + [("/etc/evil", b"e")], manifest_files=files)
    elif case == "duplicate":
        s3 = archive(GOOD + [GOOD[1]], manifest_files=files)
    elif case == "object_mismatch":    # a different object than the one the manifest describes
        s3 = archive(GOOD, archive_override=gzip.compress(b"not this archive"))
    else:                              # cut off mid-stream
        whole = archive(GOOD).objects[KEY]
        s3 = archive(GOOD, archive_override=whole[: len(whole) // 2])
    out = r2.verify_restore(s3, "b", KEY)
    assert out["ok"] is False, out
    if case == "symlink":
        assert any("not a regular file" in m for m in out["mismatches"]), out


def test_the_cli_exits_nonzero_and_says_so_when_the_manifest_is_absent(monkeypatch, capsys):
    s3 = FakeS3()
    s3.objects[KEY] = b"x"
    monkeypatch.setattr(r2, "_client", lambda: s3)
    monkeypatch.setattr("sys.argv", ["r2", "verify-restore", KEY])
    assert r2.main() == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_renders_mode_is_founder_run_only_and_not_in_cron(tmp_path):
    root = Path(__file__).resolve().parents[1]
    assert "verify-restore" not in (root / "scripts/company-os.cron").read_text()
    out = subprocess.run(["bash", str(root / "scripts/restore-test.sh"), "renders", "company-db/x.dump"],
                         env={**os.environ, "ENV_FILE": "/nonexistent", "BACKUP_LOCAL_DIR": str(tmp_path)},
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 2 and "expected a renders/" in out.stdout


def test_the_backups_reported_sha256_anchors_the_proof_outside_r2():
    # Review of #394: object and manifest sit together in R2; a writer of both could swap them as a pair.
    s3 = archive(GOOD)
    reported = json.loads(s3.objects[KEY + ".manifest.json"])["archive_sha256"]
    good = r2.verify_restore(s3, "b", KEY, reported)
    assert good["ok"] and good["anchored"] and good["archive_sha256"] == reported
    swapped = r2.verify_restore(s3, "b", KEY, "0" * 64)
    assert not swapped["ok"] and any("backup reported" in m for m in swapped["mismatches"])


def test_an_empty_archive_proves_nothing():
    assert not r2.verify_restore(archive([]), "b", KEY)["ok"]


def test_a_sparse_member_is_refused():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        info = tarfile.TarInfo("s.bin"); info.size = 3; info.type = tarfile.GNUTYPE_SPARSE
        tar.addfile(info, io.BytesIO(b"abc"))
    blob = buf.getvalue()
    s3 = FakeS3()
    s3.objects[KEY] = blob
    s3.objects[KEY + ".manifest.json"] = json.dumps({"archive_sha256": hashlib.sha256(blob).hexdigest(),
        "archive_size": len(blob), "files": [{"path": "s.bin", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}]}).encode()
    assert not r2.verify_restore(s3, "b", KEY)["ok"]


@pytest.mark.parametrize("key", ["renders/../company-db/x.tar.gz", "renders/../../out.tar.gz", "renders//a.tar.gz",
                                 "renders/./a.tar.gz"])
def test_restore_test_refuses_dot_segment_keys(key, tmp_path):
    script = (Path(r2.__file__).resolve().parent / "restore-test.sh").read_text()
    block = script[script.index('if [ "${1:-}" = renders ]; then'):script.index("SOURCE_UPLOADED=0")]
    out = subprocess.run(["bash", "-c", f'log() {{ echo "$*"; }}\nBACKUP_LOCAL_DIR={tmp_path}\nset -- renders "{key}"\n{block}'],
                         capture_output=True, text=True)
    assert out.returncode == 2 and "refused" in out.stdout
