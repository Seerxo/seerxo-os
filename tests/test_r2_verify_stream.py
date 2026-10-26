"""R2 verify: streamed SHA-256 round trip with no temporary copy, and explicit failure modes."""
import hashlib

import pytest

from scripts import r2


class Body:
    def __init__(self, data: bytes):
        self.data, self.pos, self.max_read = data, 0, 0

    def read(self, amt=-1):
        self.max_read = max(self.max_read, amt)
        chunk = self.data[self.pos:self.pos + amt]
        self.pos += len(chunk)
        return chunk


class FakeS3:
    def __init__(self, stored: bytes, meta_sha=None, size=None, body=None):
        self.stored = stored
        self.size = len(stored) if size is None else size
        self.meta = {"sha256": meta_sha} if meta_sha else {}
        self.body = Body(body if body is not None else stored)

    def head_object(self, Bucket, Key):
        return {"ContentLength": self.size, "Metadata": self.meta}

    def get_object(self, Bucket, Key):
        return {"Body": self.body}


@pytest.fixture
def local(tmp_path):
    data = b"render-bytes" * 100_000
    f = tmp_path / "archive.tar.gz"
    f.write_bytes(data)
    return f, data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_good_round_trip_passes_and_reads_in_bounded_chunks(local):
    path, data = local
    c = FakeS3(data, meta_sha=_sha(data))
    ok, msg = r2.verify_object(c, "b", "k", str(path))
    assert ok and msg.startswith("verified k")
    assert 0 < c.body.max_read <= 1024 * 1024


def test_corrupted_body_with_same_size_is_refused(local):
    path, data = local
    corrupt = bytes([data[0] ^ 1]) + data[1:]
    ok, msg = r2.verify_object(FakeS3(data, body=corrupt), "b", "k", str(path))
    assert not ok and "download sha256 mismatch" in msg


def test_truncated_stream_is_refused(local):
    path, data = local
    ok, msg = r2.verify_object(FakeS3(data, body=data[:-10]), "b", "k", str(path))
    assert not ok and "byte count" in msg


def test_wrong_checksum_in_metadata_is_refused(local):
    path, data = local
    ok, msg = r2.verify_object(FakeS3(data, meta_sha="0" * 64), "b", "k", str(path))
    assert not ok and "metadata sha256 mismatch" in msg


def test_remote_size_different_from_local_is_refused(local):
    path, data = local
    ok, msg = r2.verify_object(FakeS3(data, size=len(data) + 1), "b", "k", str(path))
    assert not ok and "size mismatch" in msg


def test_no_temporary_copy_is_written(local, monkeypatch):
    import tempfile
    path, data = local

    def boom(*a, **k):
        raise AssertionError("verify must not create a temporary copy")
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom)
    ok, _ = r2.verify_object(FakeS3(data, meta_sha=_sha(data)), "b", "k", str(path))
    assert ok


class ClosingBody(Body):
    def __init__(self, data, fail_after=None, exc=None):
        super().__init__(data)
        self.closed, self.fail_after, self.exc = False, fail_after, exc

    def read(self, amt=-1):
        if self.fail_after is not None and self.pos >= self.fail_after:
            raise self.exc
        return super().read(amt)

    def close(self):
        self.closed = True


class BodyClient(FakeS3):
    def __init__(self, body, size, meta_sha=None):
        super().__init__(b"", meta_sha=meta_sha, size=size)
        self.body = body


def test_a_read_error_mid_stream_is_a_failed_verification_and_the_body_is_closed(local):
    path, data = local
    body = ClosingBody(data, fail_after=len(data) // 2, exc=ConnectionError("reset"))
    ok, msg = r2.verify_object(BodyClient(body, len(data), _sha(data)), "b", "k", str(path))
    assert not ok and "ConnectionError" in msg
    assert body.closed


def test_a_successful_stream_is_closed_after_the_full_object_is_hashed(local):
    path, data = local
    body = ClosingBody(data)
    c = FakeS3(data, meta_sha=_sha(data))
    c.body = body
    ok, _ = r2.verify_object(c, "b", "k", str(path))
    assert ok and body.closed


def test_a_head_failure_is_a_failed_verification(local):
    path, _ = local

    class Broken(FakeS3):
        def head_object(self, Bucket, Key):
            raise RuntimeError("R2 unreachable")
    ok, msg = r2.verify_object(Broken(b""), "b", "k", str(path))
    assert not ok and "RuntimeError" in msg


def test_an_empty_local_archive_is_never_verified(tmp_path):
    empty = tmp_path / "empty.tar.gz"
    empty.write_bytes(b"")
    ok, msg = r2.verify_object(FakeS3(b""), "b", "k", str(empty))
    assert not ok and "empty" in msg


def test_an_oversized_stream_is_refused_even_when_the_head_size_matches(local):
    path, data = local
    ok, msg = r2.verify_object(BodyClient(Body(data + b"extra"), len(data), _sha(data)), "b", "k", str(path))
    assert not ok and "byte count" in msg


def test_missing_metadata_still_requires_the_streamed_bytes_to_match(local):
    path, data = local
    ok_good, _ = r2.verify_object(FakeS3(data), "b", "k", str(path))            # no metadata, correct bytes
    corrupt = bytes([data[0] ^ 1]) + data[1:]
    ok_bad, msg = r2.verify_object(FakeS3(data, body=corrupt), "b", "k", str(path))
    assert ok_good and not ok_bad and "download sha256 mismatch" in msg
