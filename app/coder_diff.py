"""Import a coder's changes back into the control plane, the reverse of coder_source.

Unused Stage C prerequisite (not wired; the worker stays Stage B). The coder workspace is
untrusted: everything in it is data. The control plane keeps its own pristine export
(`base`), compares the workspace against it, and accepts only:

- modified files that were exported, plus new files the task scope explicitly allows;
- regular, single-link files under paths that pass coder_source's safety rules
  (no .git, .env, keys, governance or workflow files);
- UTF-8 text, at most 20 changed files, 256 KiB per file, 1 MiB in total.

Deletions, symlinks, hard links, devices and mode-only changes are refused. The result is
a unified diff and its fingerprint; `apply_to_worktree` applies exactly that diff, and only
when the fingerprint still matches the one the independent reviewer approved.
"""
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from app.coder_source import _safe_path
from app.stage_c import diff_fingerprint

MAX_FILES = 20
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024


class RejectedChange(ValueError):
    """The workspace contains something the control plane will not import."""


def _env() -> dict:
    # Same hardening as coder_source: no inherited Git config, hooks, auth or lazy fetch.
    return {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C", "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "", "GIT_NO_REPLACE_OBJECTS": "1"}


def _tree(root: Path) -> dict[str, Path]:
    """Every entry under root as {relative posix path: path}; anything but a plain file fails."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                if stat.S_ISLNK(st.st_mode):
                    raise RejectedChange(f"{rel}: symlinked directory")
                continue
            if not stat.S_ISREG(st.st_mode):
                raise RejectedChange(f"{rel}: not a regular file (symlink, device or fifo)")
            if st.st_nlink != 1:
                raise RejectedChange(f"{rel}: hard-linked file")
            files[rel] = full
    return files


def _text(rel: str, path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise RejectedChange(f"{rel}: larger than {MAX_FILE_BYTES} bytes")
    if b"\0" in data:
        raise RejectedChange(f"{rel}: binary content is not reviewable")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise RejectedChange(f"{rel}: not UTF-8 text") from None
    return data


def import_changes(base: Path, workspace: Path, manifest: dict, allowed_new: list[str] | None = None) -> dict:
    """Diff the untrusted workspace against the control plane's pristine `base` export.

    `manifest` is coder_source.export_source's return value for that export. Returns
    {"commit", "diff" (bytes), "fingerprint", "modified", "added"}; raises RejectedChange.
    """
    allowed_new = set(allowed_new or [])
    exported = manifest["files"]
    base_files, work_files = _tree(Path(base)), _tree(Path(workspace))
    # The base must be exactly what was exported; a tampered base would hide changes.
    if set(base_files) != set(exported) or any(
            hashlib.sha256(base_files[p].read_bytes()).hexdigest() != h for p, h in exported.items()):
        raise RejectedChange("base export does not match its manifest")
    if missing := sorted(set(exported) - set(work_files)):
        raise RejectedChange(f"deleted files are not accepted: {', '.join(missing)}")
    modified, added, total = [], [], 0
    for rel in sorted(work_files):
        if not _safe_path(rel):
            raise RejectedChange(f"{rel}: restricted path")
        if rel in exported:
            data = _text(rel, work_files[rel])
            if hashlib.sha256(data).hexdigest() == exported[rel]:
                continue
            modified.append(rel)
        elif rel in allowed_new:
            data = _text(rel, work_files[rel])
            added.append(rel)
        else:
            raise RejectedChange(f"{rel}: new file outside the task scope")
        total += len(data)
    changed = modified + added
    if not changed:
        raise RejectedChange("no changes")
    if len(changed) > MAX_FILES or total > MAX_TOTAL_BYTES:
        raise RejectedChange(f"change exceeds {MAX_FILES} files / {MAX_TOTAL_BYTES} bytes")

    # Render with git in a private scratch tree holding copies, never the workspace itself.
    with tempfile.TemporaryDirectory(prefix="coder-diff-") as tmp:
        for side, src in (("a", base_files), ("b", work_files)):
            for rel in changed:
                if rel in src:
                    dest = Path(tmp, side, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src[rel], dest)
            Path(tmp, side).mkdir(exist_ok=True)
        out = subprocess.run(["git", "--no-pager", "diff", "--no-index", "--no-color", "--no-ext-diff",
                              "--no-textconv", "--no-renames",
                              # Directories a/ and b/ act as the prefixes: headers read a/<path>, b/<path>.
                              "--src-prefix=", "--dst-prefix=", "--", "a", "b"],
                             cwd=tmp, env=_env(), capture_output=True, timeout=30)
    if out.returncode not in (0, 1):
        raise RejectedChange(f"diff failed: {out.stderr.decode(errors='replace')[:200]}")
    diff = out.stdout
    return {"commit": manifest["commit"], "diff": diff, "fingerprint": diff_fingerprint(diff),
            "modified": modified, "added": added}


def apply_to_worktree(worktree: Path, diff: bytes, approved_fingerprint: str) -> list[str]:
    """Apply exactly the reviewed diff to the control plane's worktree. Returns touched paths."""
    if diff_fingerprint(diff) != approved_fingerprint:
        raise RejectedChange("diff does not match the approved fingerprint")
    run = lambda *args: subprocess.run(["git", "--no-pager", "-C", str(worktree), "apply", *args],
                                       input=diff, env=_env(), capture_output=True, timeout=30)
    paths = [line.split(b"\t")[-1].decode() for line in run("--numstat").stdout.splitlines() if line]
    if not paths or not all(_safe_path(p) for p in paths):
        raise RejectedChange("patch touches a restricted or unparseable path")
    for args in (("--check",), ()):
        result = run(*args)
        if result.returncode:
            raise RejectedChange(f"patch does not apply cleanly: {result.stderr.decode(errors='replace')[:200]}")
    return paths
