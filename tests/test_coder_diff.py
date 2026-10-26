"""Importing a coder's changes: only scoped, plain, reviewable text crosses back."""
import os
import subprocess

import pytest

from app import coder_diff, coder_source, stage_c


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / "app").mkdir()
    (repo / "app" / "a.py").write_text("x = 1\n")
    (repo / "app" / "noeol.py").write_text("y = 2")  # no trailing newline
    git("add", ".")
    git("-c", "user.name=f", "-c", "user.email=f@example.invalid", "commit", "-qm", "base")
    sha = git("rev-parse", "HEAD")
    paths = ["app/a.py", "app/noeol.py"]
    manifest = coder_source.export_source(repo, sha, paths, tmp_path / "base")
    coder_source.export_source(repo, sha, paths, tmp_path / "work")
    return repo, sha, manifest, tmp_path / "base", tmp_path / "work"


def test_modified_and_allowed_new_files_round_trip_into_the_worktree(setup, tmp_path):
    repo, sha, manifest, base, work = setup
    (work / "app" / "a.py").write_text("x = 2\n")
    (work / "app" / "noeol.py").write_text("y = 3")
    (work / "app" / "new.py").write_text("z = 1\n")
    out = coder_diff.import_changes(base, work, manifest, allowed_new=["app/new.py"])
    assert out["modified"] == ["app/a.py", "app/noeol.py"] and out["added"] == ["app/new.py"]
    assert out["fingerprint"] == stage_c.diff_fingerprint(out["diff"]) and out["commit"] == sha
    # The worktree gets exactly the coder's bytes, including a missing final newline.
    wt = stage_c.prepare_worktree(repo, tmp_path / "ws", "TASK-0001", sha)
    assert sorted(coder_diff.apply_to_worktree(wt, out["diff"], out["fingerprint"])) == sorted(out["modified"] + out["added"])
    for rel in ("app/a.py", "app/noeol.py", "app/new.py"):
        assert (wt / rel).read_bytes() == (work / rel).read_bytes()


def test_a_changed_diff_is_not_applied(setup, tmp_path):
    repo, sha, manifest, base, work = setup
    (work / "app" / "a.py").write_text("x = 2\n")
    out = coder_diff.import_changes(base, work, manifest)
    wt = stage_c.prepare_worktree(repo, tmp_path / "ws", "TASK-0002", sha)
    # Break caught: the reviewer approved F, something else gets applied.
    with pytest.raises(coder_diff.RejectedChange):
        coder_diff.apply_to_worktree(wt, out["diff"] + b"\n", out["fingerprint"])
    assert (wt / "app" / "a.py").read_text() == "x = 1\n"


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "unscoped_new", "env_file", "git_dir",
                                    "deletion", "binary", "oversize", "fifo", "governance"])
def test_untrusted_workspace_content_is_rejected(setup, attack):
    _, _, manifest, base, work = setup
    (work / "app" / "a.py").write_text("x = 2\n")  # a legitimate change alongside the attack
    allowed = []
    if attack == "symlink":
        (work / "app" / "link.py").symlink_to("/etc/passwd")
        allowed = ["app/link.py"]
    elif attack == "hardlink":
        os.link(work / "app" / "a.py", work / "app" / "hard.py")
        allowed = ["app/hard.py"]
    elif attack == "unscoped_new":
        (work / "app" / "extra.py").write_text("print('hi')\n")
    elif attack == "env_file":
        (work / ".env").write_text("SECRET=1\n")
        allowed = [".env"]
    elif attack == "git_dir":
        (work / ".git").mkdir()
        (work / ".git" / "config").write_text("[core]\n")
        allowed = [".git/config"]
    elif attack == "deletion":
        (work / "app" / "noeol.py").unlink()
    elif attack == "binary":
        (work / "app" / "a.py").write_bytes(b"x = 2\n\0\0")
    elif attack == "oversize":
        (work / "app" / "a.py").write_text("#" * (coder_diff.MAX_FILE_BYTES + 1))
    elif attack == "fifo":
        os.mkfifo(work / "app" / "pipe.py")
        allowed = ["app/pipe.py"]
    elif attack == "governance":
        (work / "CLAUDE.md").write_text("new rules\n")
        allowed = ["CLAUDE.md"]
    with pytest.raises(coder_diff.RejectedChange):
        coder_diff.import_changes(base, work, manifest, allowed_new=allowed)


def test_tampered_base_and_no_op_are_rejected(setup):
    _, _, manifest, base, work = setup
    with pytest.raises(coder_diff.RejectedChange, match="no changes"):
        coder_diff.import_changes(base, work, manifest)
    (base / "app" / "a.py").write_text("x = 999\n")  # base no longer what was exported
    (work / "app" / "a.py").write_text("x = 2\n")
    with pytest.raises(coder_diff.RejectedChange, match="manifest"):
        coder_diff.import_changes(base, work, manifest)
