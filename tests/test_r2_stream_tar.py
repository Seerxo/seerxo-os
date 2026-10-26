"""Streamed renders archive (scripts/r2.py stream-tar): no local copy, one part buffered, retries resend the
same bytes, any failure aborts the multipart upload, and a source change fails the run."""
import hashlib
import io
import json
import os
import time
import tarfile

import pytest

from scripts import r2


class FakeS3:
    def __init__(self, fail_parts=(), on_part=None):
        self.fail_parts = list(fail_parts)    # part numbers that fail once each, in order
        self.on_part = on_part
        self.sent = []                        # (part number, bytes) of every attempt
        self.objects, self.aborted, self.completed = {}, [], []

    def create_multipart_upload(self, Bucket, Key):
        return {"UploadId": "u1"}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.sent.append((PartNumber, Body))
        if self.on_part:
            self.on_part(PartNumber)
        if PartNumber in self.fail_parts:
            self.fail_parts.remove(PartNumber)
            raise ConnectionError("reset")
        return {"ETag": f"e{PartNumber}"}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        parts = MultipartUpload["Parts"]
        assert [p["PartNumber"] for p in parts] == list(range(1, len(parts) + 1))
        last = {}
        for number, body in self.sent:
            last[number] = body
        self.objects[Key] = b"".join(last[p["PartNumber"]] for p in parts)
        self.completed.append(Key)

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.aborted.append(Key)

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key]), "Metadata": {}}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture
def src(tmp_path):
    root = tmp_path / "renders"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "v1.mp4").write_bytes(os.urandom(300_000))   # incompressible: several parts
    (root / "a" / "note.txt").write_bytes(b"hello" * 1000)
    (root / "top.bin").write_bytes(os.urandom(50_000))
    return root


PART = 64 * 1024


def test_archive_is_streamed_hashed_and_restorable(src):
    s3 = FakeS3()
    out = r2.stream_tar(s3, "b", "renders/weekly/x.tar.gz", str(src), part_size=PART)
    data = s3.objects["renders/weekly/x.tar.gz"]
    assert out["sha256"] == hashlib.sha256(data).hexdigest() and out["size"] == len(data) and out["files"] == 3
    assert len(s3.sent) > 2 and all(len(b) == PART for _, b in s3.sent[:-1])   # equal parts except the last
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        got = {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}
    assert got == {p: (src / p).read_bytes() for p in ("a/b/v1.mp4", "a/note.txt", "top.bin")}
    manifest = json.loads(s3.objects["renders/weekly/x.tar.gz.manifest.json"])
    assert manifest["archive_sha256"] == out["sha256"]
    assert {f["path"]: f["sha256"] for f in manifest["files"]} == {
        p: hashlib.sha256((src / p).read_bytes()).hexdigest() for p in got}
    assert r2.verify_remote(s3, "b", "renders/weekly/x.tar.gz", out["sha256"], out["size"], out["files"])[0]


def test_a_failed_part_is_retried_with_identical_bytes(src):
    s3 = FakeS3(fail_parts=[2])
    out = r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    tries = [body for number, body in s3.sent if number == 2]
    assert len(tries) == 2 and tries[0] == tries[1]
    assert hashlib.sha256(s3.objects["k"]).hexdigest() == out["sha256"] and not s3.aborted


def test_exhausted_retries_abort_the_upload_and_record_nothing(src):
    s3 = FakeS3(fail_parts=[2] * r2.PART_ATTEMPTS)
    with pytest.raises(ConnectionError):
        r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    assert s3.aborted == ["k"] and not s3.completed and "k.manifest.json" not in s3.objects


def test_a_file_changed_during_the_run_fails_and_aborts(src):
    def touch(number):
        if number == 1:
            (src / "top.bin").write_bytes(b"rewritten")
    s3 = FakeS3(on_part=touch)
    with pytest.raises(r2.SourceChanged):
        r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    assert s3.aborted == ["k"] and not s3.completed


def test_a_symlink_is_refused_before_anything_is_uploaded(src):
    (src / "link").symlink_to(src / "top.bin")
    s3 = FakeS3()
    with pytest.raises(r2.SourceChanged):
        r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    assert not s3.sent and not s3.completed


def test_verify_remote_rejects_a_wrong_member_count_and_a_corrupt_object(src):
    s3 = FakeS3()
    out = r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    assert not r2.verify_remote(s3, "b", "k", out["sha256"], out["size"], out["files"] + 1)[0]
    s3.objects["k"] = s3.objects["k"][:-10] + b"x" * 10
    assert not r2.verify_remote(s3, "b", "k", out["sha256"], out["size"], out["files"])[0]


def test_a_directory_symlink_is_refused_not_silently_skipped(src, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "clip.mp4").write_bytes(b"x" * 100)
    (src / "a" / "linked").symlink_to(outside, target_is_directory=True)
    s3 = FakeS3()
    with pytest.raises(r2.SourceChanged, match="not a directory"):
        r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    assert not s3.sent


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable directories")
def test_an_unreadable_subtree_fails_instead_of_being_left_out(src):
    locked = src / "a" / "b"
    locked.chmod(0)
    try:
        s3 = FakeS3()
        with pytest.raises(r2.SourceChanged, match="cannot read"):
            r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
        assert not s3.sent
    finally:
        locked.chmod(0o755)


@pytest.mark.parametrize("make", ["missing", "file", "symlink", "empty"])
def test_a_missing_or_empty_root_is_refused(tmp_path, make):
    root = tmp_path / "root"
    if make == "file":
        root.write_bytes(b"x")
    elif make == "symlink":
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "f").write_bytes(b"x")
        root.symlink_to(tmp_path / "real", target_is_directory=True)
    elif make == "empty":
        root.mkdir()
    with pytest.raises(r2.SourceChanged):
        r2.stream_tar(FakeS3(), "b", "k", str(root), part_size=PART)


def test_a_same_size_rewrite_with_its_mtime_put_back_is_detected(src):
    target = src / "top.bin"
    before = target.stat()

    def rewrite(number):
        if number == 1:
            # Linux stamps ctime from a coarse clock (a few ms): let it tick past the file's creation first.
            time.sleep(0.05)
            target.write_bytes(os.urandom(before.st_size))                  # same size, new content
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))   # mtime restored; ctime moves
    s3 = FakeS3(on_part=rewrite)
    with pytest.raises(r2.SourceChanged):
        r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    assert s3.aborted == ["k"] and not s3.completed


def test_a_manifest_failure_keeps_the_completed_archive_and_says_so(src):
    class NoManifest(FakeS3):
        def put_object(self, Bucket, Key, Body):
            raise ConnectionError("reset")
    s3 = NoManifest()
    with pytest.raises(r2.ManifestMissing) as missing:
        r2.stream_tar(s3, "b", "k", str(src), part_size=PART)
    assert s3.completed == ["k"] and not s3.aborted
    assert missing.value.result["sha256"] == hashlib.sha256(s3.objects["k"]).hexdigest()
