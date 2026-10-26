#!/usr/bin/env python3
"""Cloudflare R2 transport for backups. R2 is S3-compatible, so boto3 is enough and
no extra system binary is needed on the host.

Credential-free by design: with R2_* unset every command exits 10 (NOT_CONFIGURED),
which backup.sh treats as "off-site leg missing" rather than success.

    python -m scripts.r2 upload  <local> <key>
    python -m scripts.r2 verify  <local> <key>     # re-download, compare sha256
    python -m scripts.r2 list    [prefix]
    python -m scripts.r2 download <listed-key> <local>   # key exactly as `list` prints it
    python -m scripts.r2 delete  <key>
    python -m scripts.r2 check                     # credentials + bucket reachable
    python -m scripts.r2 prune   <prefix> <keep>   # keep newest <keep> (>=1), delete older
    python -m scripts.r2 usage                     # total bytes under R2_PREFIX
    python -m scripts.r2 stream-tar <dir> <key>    # tar.gz <dir> straight into a new object; prints "<sha256> <size> <files>"
    python -m scripts.r2 verify-remote <key> <sha256> <size> [files]   # full re-read (and tar read) of a streamed archive
    python -m scripts.r2 verify-restore <key>      # stream a streamed archive and check every file against its manifest
"""
import hashlib
import json
import os
import re
import stat
import sys
import tarfile

NOT_CONFIGURED = 10


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _client():
    import boto3
    from botocore.config import Config

    endpoint = _env("R2_ENDPOINT") or (
        f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com" if _env("R2_ACCOUNT_ID") else ""
    )
    key_id, secret = _env("R2_ACCESS_KEY_ID"), _env("R2_SECRET_ACCESS_KEY")
    if not (endpoint and key_id and secret and _env("R2_BUCKET")):
        print("R2 not configured (need R2_ENDPOINT or R2_ACCOUNT_ID, key, secret, bucket)",
              file=sys.stderr)
        sys.exit(NOT_CONFIGURED)
    return boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=key_id, aws_secret_access_key=secret,
        region_name="auto", config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_object(c, bucket: str, key: str, local: str) -> tuple[bool, str]:
    """Round-trip one object without writing a copy to disk.

    Policy, explicit: the local file must be non-empty; the remote size must equal the local size;
    the streamed byte count must equal that size (truncated or oversized bodies fail); the streamed
    SHA-256 must equal the local SHA-256. Remote metadata sha256 is optional, but if present it must
    match. Missing metadata is therefore never treated as verified on its own: the streamed bytes
    must still prove the content. Any read or network error returns failure, never success.
    The body is read in 1 MiB chunks and always closed."""
    try:
        want, local_size = sha256(local), os.path.getsize(local)
    except OSError as exc:
        return False, f"cannot read local file {local}: {exc}"
    if local_size == 0:
        return False, f"local archive is empty: {local}"
    return verify_remote(c, bucket, key, want, local_size)


def verify_remote(c, bucket: str, key: str, want: str, want_size: int, members: int | None = None) -> tuple[bool, str]:
    """verify_object's policy against an expected SHA-256 and size instead of a local file (a streamed
    archive has no local copy). With `members`, the body is also read as a tar.gz while it is hashed,
    and must decompress cleanly and hold exactly that many regular files."""
    try:
        head = c.head_object(Bucket=bucket, Key=key)
        size = int(head["ContentLength"])
        remote_meta = (head.get("Metadata") or {}).get("sha256")
        if remote_meta and remote_meta != want:
            return False, f"metadata sha256 mismatch: remote={remote_meta} local={want}"
        if size != want_size:
            return False, f"size mismatch: remote={size} local={want_size}"
        body = c.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            reader = _HashingReader(body)
            files = None
            if members is not None:
                with tarfile.open(fileobj=reader, mode="r|gz") as tar:
                    files = 0
                    for m in tar:
                        if m.isfile():
                            files += 1
                            data = tar.extractfile(m)
                            for _ in iter(lambda: data.read(1024 * 1024), b""):
                                pass
            for _ in iter(lambda: reader.read(1024 * 1024), b""):   # gzip trailer padding, if any
                pass
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
    except Exception as exc:  # noqa: BLE001 - any transport or read failure is a failed verification
        return False, f"round trip failed for {key}: {type(exc).__name__}: {exc}"
    if reader.count != size:
        return False, f"stream byte count {reader.count} differs from object size {size} (truncated or oversized)"
    if reader.hash.hexdigest() != want:
        return False, f"download sha256 mismatch: got={reader.hash.hexdigest()} want={want}"
    if members is not None and files != members:
        return False, f"archive holds {files} files, manifest has {members}"
    return True, f"verified {key} sha256={want} size={size}" + (f" files={files}" if files is not None else "")


class _HashingReader:
    def __init__(self, raw):
        self.raw, self.hash, self.count = raw, hashlib.sha256(), 0

    def read(self, n=-1):
        chunk = self.raw.read(n)
        self.hash.update(chunk)
        self.count += len(chunk)
        return chunk


# --------------------------------------------------------------------------- streamed archive
# The weekly renders volume (~17 GB) no longer fits on the host twice, so it is never written locally:
# tar -> gzip -> SHA-256 -> one multipart upload, one part at a time. The only buffer is the part being
# sent (PART_SIZE in memory), so a failed part is retried with exactly the same bytes, never by re-reading
# a gzip stream that has moved on. R2 needs equal part sizes except the last.
PART_SIZE = 64 * 1024 * 1024
PART_ATTEMPTS = 3


class SourceChanged(Exception):
    pass


class ManifestMissing(Exception):
    """The archive object was completed (and is kept), but its manifest could not be written."""
    def __init__(self, result: dict):
        super().__init__("archive completed but its manifest upload failed; the object is kept")
        self.result = result


class _MultipartSink:
    def __init__(self, c, bucket: str, key: str, upload_id: str, part_size: int):
        self.c, self.bucket, self.key, self.upload_id, self.part_size = c, bucket, key, upload_id, part_size
        self.buf, self.parts, self.hash, self.size = bytearray(), [], hashlib.sha256(), 0

    def write(self, data) -> int:
        self.hash.update(data)
        self.size += len(data)
        self.buf += data
        while len(self.buf) >= self.part_size:
            self._send(bytes(self.buf[:self.part_size]))
            del self.buf[:self.part_size]
        return len(data)

    def _send(self, body: bytes) -> None:
        number = len(self.parts) + 1
        for attempt in range(PART_ATTEMPTS):
            try:
                r = self.c.upload_part(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id,
                                       PartNumber=number, Body=body)
                break
            except Exception:  # noqa: BLE001 - retried with the same bytes, then the upload is aborted
                if attempt == PART_ATTEMPTS - 1:
                    raise
        self.parts.append({"PartNumber": number, "ETag": r["ETag"]})

    def finish(self) -> None:
        if self.buf or not self.parts:
            self._send(bytes(self.buf))
            self.buf.clear()


def _identity(st: os.stat_result) -> tuple:
    """What must not change while a file is archived. ctime catches a same-size rewrite whose mtime was put back."""
    # ponytail: ctime has the kernel's coarse granularity (a few ms); a rewrite within that window of the file's
    # previous change is invisible here. Render files are written long before the weekly run.
    return (stat.S_IFMT(st.st_mode), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _walk_error(error: OSError):
    raise SourceChanged(f"cannot read the source tree: {error}") from error


def _snapshot(root: str) -> list[tuple[str, tuple]]:
    """(relative path, identity) of every regular file, sorted. The whole tree must be readable and hold only
    real directories and regular files: a skipped subtree or a symlink would leave content out of an archive
    that its own SHA-256 and file count would still call complete."""
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise SourceChanged(f"source is not a directory: {root}")
    except OSError as error:
        raise SourceChanged(f"source is missing or unreadable: {error}") from error
    out = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
        dirnames.sort()
        for name in dirnames:
            if not stat.S_ISDIR(os.lstat(os.path.join(dirpath, name)).st_mode):
                raise SourceChanged(f"not a directory: {os.path.relpath(os.path.join(dirpath, name), root)}")
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise SourceChanged(f"not a regular file: {os.path.relpath(path, root)}")
            out.append((os.path.relpath(path, root), _identity(st)))
    if not out:
        raise SourceChanged(f"source holds no files: {root}")
    return out


def stream_tar(c, bucket: str, key: str, root: str, part_size: int = PART_SIZE) -> dict:
    """Archive the files present when the run starts straight into a new object. A file that changes,
    shrinks or disappears before the upload completes fails the run: the multipart upload is aborted, so
    no partial object is ever visible. Files created during the run are not included (next week's run).
    A `<key>.manifest.json` with every file's size, mtime and SHA-256 is written only after completion; if
    only that write fails, ManifestMissing carries the result and the completed archive is kept."""
    files = _snapshot(root)
    upload_id = c.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    sink = _MultipartSink(c, bucket, key, upload_id, part_size)
    manifest = []
    try:
        with tarfile.open(fileobj=sink, mode="w|gz") as tar:
            for rel, identity in files:
                fd = os.open(os.path.join(root, rel), os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as fh:
                    if _identity(os.fstat(fd)) != identity:
                        raise SourceChanged(f"changed before it was archived: {rel}")
                    info = tar.gettarinfo(arcname=rel, fileobj=fh)
                    reader = _HashingReader(fh)
                    tar.addfile(info, reader)    # raises if the file is shorter than its size
                    if _identity(os.fstat(fd)) != identity:
                        raise SourceChanged(f"changed while it was archived: {rel}")
                manifest.append({"path": rel, "size": identity[3], "mtime_ns": identity[4],
                                 "sha256": reader.hash.hexdigest()})
        sink.finish()
        after = dict(_snapshot(root))
        changed = [rel for rel, identity in files if after.get(rel) != identity]
        if changed:
            raise SourceChanged(f"{len(changed)} files changed or disappeared while archived, e.g. {changed[0]}")
        c.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id,
                                    MultipartUpload={"Parts": sink.parts})
    except BaseException:
        c.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise
    digest = sink.hash.hexdigest()
    result = {"sha256": digest, "size": sink.size, "files": len(manifest)}
    try:
        c.put_object(Bucket=bucket, Key=key + ".manifest.json",
                     Body=json.dumps({"archive_sha256": digest, "archive_size": sink.size, "files": manifest}).encode())
    except Exception as error:  # noqa: BLE001 - the completed archive is kept and reported, never claimed aborted
        raise ManifestMissing(result) from error
    return result


def _safe_member(name) -> bool:
    """A relative, normalized path inside the archive root: no absolute path, no '..', nothing odd."""
    return (isinstance(name, str) and name != "" and not name.startswith("/") and "\\" not in name
            and os.path.normpath(name) == name and ".." not in name.split("/"))


def verify_restore(c, bucket: str, key: str, expected_sha256: str | None = None) -> dict:
    """Restore-usability proof for a streamed archive without disk: read `<key>.manifest.json`, stream the
    object once (tar 'r|gz', bounded memory) and check every member is a regular file at a safe path listed
    in the manifest exactly once, with the manifest's size and SHA-256; every manifest entry must be seen,
    and the whole object must match the manifest's archive SHA-256 and size. Nothing is written to disk.
    The manifest lives next to the object, so a writer of both could swap them together: `expected_sha256`
    (the SHA-256 the backup reported to Company OS) anchors the proof outside R2."""
    raw = c.get_object(Bucket=bucket, Key=key + ".manifest.json")["Body"]
    try:
        manifest = json.loads(raw.read())
    finally:
        getattr(raw, "close", lambda: None)()
    want, mismatches, extra, seen = {}, [], [], set()
    for entry in manifest["files"]:
        path = entry["path"]
        if not _safe_member(path) or path in want:
            mismatches.append(f"manifest: unsafe or duplicate path {path!r}")
            continue
        want[path] = (int(entry["size"]), entry["sha256"])
    files = total = 0
    body = c.get_object(Bucket=bucket, Key=key)["Body"]
    reader = _HashingReader(body)
    try:
        with tarfile.open(fileobj=reader, mode="r|gz") as tar:
            for member in tar:
                name = member.name
                if not member.isfile() or member.issparse() or not _safe_member(name):
                    mismatches.append(f"{name!r}: not a regular file at a safe path")
                    continue
                digest, size = hashlib.sha256(), 0
                data = tar.extractfile(member)
                for chunk in iter(lambda: data.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                if name in seen:
                    mismatches.append(f"{name}: duplicate member")
                    continue
                if name not in want:
                    extra.append(name)
                    continue
                seen.add(name)
                files, total = files + 1, total + size
                if (size, digest.hexdigest()) != want[name]:
                    mismatches.append(f"{name}: size or sha256 differs from the manifest")
        for _ in iter(lambda: reader.read(1024 * 1024), b""):   # gzip trailer padding, if any
            pass
    except (tarfile.TarError, OSError, EOFError) as error:   # zlib errors surface as tarfile/OSError
        mismatches.append(f"archive unreadable: {type(error).__name__}: {error}")
    finally:
        getattr(body, "close", lambda: None)()
    if (reader.count, reader.hash.hexdigest()) != (manifest.get("archive_size"), manifest.get("archive_sha256")):
        mismatches.append("archive: size or sha256 differs from the manifest")
    if expected_sha256 is not None and manifest.get("archive_sha256") != expected_sha256:
        mismatches.append("archive: manifest sha256 differs from the SHA-256 the backup reported")
    missing = sorted(set(want) - seen)
    return {"files": files, "bytes": total, "archive_sha256": manifest.get("archive_sha256"),
            "anchored": expected_sha256 is not None, "mismatches": mismatches, "missing": missing, "extra": extra,
            "ok": files > 0 and not (mismatches or missing or extra)}


def prune(client, bucket: str, prefix: str, keep: int) -> list[str]:
    """Delete all but the newest `keep` objects under one target/class prefix.

    Never deletes the last copy (keep is floored at 1) and never leaves the prefix, so a
    bug here can shrink one retention window but cannot empty the archive.
    """
    keep = max(1, int(keep))
    objs = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objs.extend(page.get("Contents", []))
    objs.sort(key=lambda o: o["LastModified"], reverse=True)
    doomed = [o["Key"] for o in objs[keep:] if o["Key"].startswith(prefix)]
    for key in doomed:
        client.delete_object(Bucket=bucket, Key=key)
    return doomed


def usage(client, bucket: str, prefix: str) -> int:
    total = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        total += sum(o["Size"] for o in page.get("Contents", []))
    return total


def _key(key: str) -> str:
    prefix = _env("R2_PREFIX")
    return f"{prefix.rstrip('/')}/{key.lstrip('/')}" if prefix else key


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    bucket = _env("R2_BUCKET")

    if cmd == "check":
        _client().head_bucket(Bucket=bucket)
        print(f"R2 OK bucket={bucket} prefix={_env('R2_PREFIX') or '(none)'}")
        return 0

    if cmd == "upload":
        local, key = sys.argv[2], _key(sys.argv[3])
        digest = sha256(local)
        # The checksum travels with the object so verification never depends on
        # a local record that could itself be lost.
        _client().upload_file(local, bucket, key,
                              ExtraArgs={"Metadata": {"sha256": digest}})
        print(f"uploaded {key} sha256={digest}")
        return 0

    if cmd == "verify":
        local, key = sys.argv[2], _key(sys.argv[3])
        ok, message = verify_object(_client(), bucket, key, local)
        if not ok:
            print(f"VERIFY FAILED {message}", file=sys.stderr)
            return 1
        print(message)
        return 0

    if cmd == "stream-tar":
        try:
            out = stream_tar(_client(), bucket, _key(sys.argv[3]), sys.argv[2])
        except ManifestMissing as missing:
            out = missing.result
            print(f"{out['sha256']} {out['size']} {out['files']}")
            print(f"MANIFEST MISSING {missing}", file=sys.stderr)
            return 3
        print(f"{out['sha256']} {out['size']} {out['files']}")
        return 0

    if cmd == "verify-remote":
        key = _key(sys.argv[2])
        members = int(sys.argv[5]) if len(sys.argv) > 5 else None
        ok, message = verify_remote(_client(), bucket, key, sys.argv[3], int(sys.argv[4]), members)
        if not ok:
            print(f"VERIFY FAILED {message}", file=sys.stderr)
            return 1
        print(message)
        return 0

    if cmd == "verify-restore":
        try:
            expected = sys.argv[3] if len(sys.argv) > 3 else None
            if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("expected sha256 must be 64 lowercase hex")
            out = verify_restore(_client(), bucket, _key(sys.argv[2]), expected)
        except Exception as error:  # noqa: BLE001 - missing manifest, transport error: a failed proof, never a pass
            out = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        print(json.dumps(out))
        return 0 if out["ok"] else 1

    if cmd == "list":
        prefix = _key(sys.argv[2]) if len(sys.argv) > 2 else _env("R2_PREFIX")
        paginator = _client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                print(f"{obj['LastModified'].isoformat()}\t{obj['Size']}\t{obj['Key']}")
        return 0

    if cmd == "download":
        # The listed key already carries R2_PREFIX, so it is used as given.
        _client().download_file(bucket, sys.argv[2], sys.argv[3])
        print(f"downloaded {sys.argv[2]}")
        return 0

    if cmd == "delete":
        key = _key(sys.argv[2])
        _client().delete_object(Bucket=bucket, Key=key)
        print(f"deleted {key}")
        return 0

    if cmd == "prune":
        prefix = _key(sys.argv[2]).rstrip("/") + "/"
        for key in prune(_client(), bucket, prefix, int(sys.argv[3])):
            print(f"pruned {key}")
        return 0

    if cmd == "usage":
        total = usage(_client(), bucket, _env("R2_PREFIX"))
        print(f"R2 usage bytes={total} gb={total / 1e9:.2f}")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
