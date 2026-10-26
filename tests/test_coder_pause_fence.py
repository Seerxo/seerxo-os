"""Use real file locks and the real founder commit path, without Docker or a provider."""
import fcntl
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import control, models
from tests.test_coder_launch import CFG, L, _job, _load
from tests.test_coder_launch_pause import fence


def enable_api_fence(path, monkeypatch):
    monkeypatch.setenv("CODER_PAUSE_FENCE_ENABLED", "true")
    monkeypatch.setattr(control, "CODER_PAUSE_FENCE", str(path))
    monkeypatch.setattr(control, "_FENCE_UID", os.getuid())
    monkeypatch.setattr(control, "_FENCE_TIMEOUT", 0.05)


def test_helper_death_cannot_let_founder_pause_commit_before_permission_grant(L, tmp_path, monkeypatch):
    job = _job(L)
    mod = _load()
    path = fence(mod, tmp_path, monkeypatch)
    enable_api_fence(path, monkeypatch)
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    original_popen, original_worker = subprocess.Popen, L.as_worker
    children, events = [], []
    monkeypatch.setattr(L, "pause_guard", mod.pause_guard)

    def popen(argv, **kwargs):
        if argv[:3] == ["docker", "exec", "-i"]:
            child = original_popen([sys.executable, "-u", "-c",
                "import sys; print('READY', flush=True); "
                "assert sys.stdin.readline() == 'START\\n'; print('ALLOW', flush=True)"], **kwargs)
            children.append(child)
            return child
        return original_popen(argv, **kwargs)

    with Session(engine) as db:
        control.set_agents_paused(db, False, actor="founder", reason="test")

        def as_worker(*cmd, **kwargs):
            if cmd[0] == "mkdir" and str(job / "done") in cmd and not events:
                assert children[0].wait(timeout=2) == 0
                with pytest.raises(control.PauseUnavailable):
                    control.set_agents_paused(db, True, actor="founder", reason="race")
                assert not control.agents_paused(db)
                events.append("pause_cannot_commit_during_grant")
            return original_worker(*cmd, **kwargs)

        def runner(*args):
            events.append("admitted")
            return {"exit": 0, "timed_out": False}

        monkeypatch.setattr(L.subprocess, "Popen", popen)
        monkeypatch.setattr(L, "as_worker", as_worker)
        assert L.handle("TASK-0101", CFG, runner=runner)["exit"] == 0
        control.set_agents_paused(db, True, actor="founder", reason="after grant")
        assert control.agents_paused(db)
    assert events == ["pause_cannot_commit_during_grant", "admitted"]
    engine.dispose()


@pytest.mark.parametrize("kind", ["missing", "symlink", "writable", "directory", "hardlink", "wrong-owner"])
def test_invalid_fences_refuse_both_pause_commit_and_root_admission(tmp_path, monkeypatch, kind):
    mod = _load()
    path = fence(mod, tmp_path, monkeypatch)
    enable_api_fence(path, monkeypatch)
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        path.rename(tmp_path / "target")
        path.symlink_to(tmp_path / "target")
    elif kind == "writable":
        path.chmod(0o644)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "hardlink":
        os.link(path, tmp_path / "other")
    else:
        monkeypatch.setattr(mod, "_FENCE_UID", os.getuid() + 1)
        monkeypatch.setattr(control, "_FENCE_UID", os.getuid() + 1)
    with pytest.raises((mod.Refused, OSError)):
        with mod.pause_guard():
            pytest.fail("invalid root fence admitted a coder")
    with pytest.raises(control.PauseUnavailable):
        with control.coder_pause_fence():
            pytest.fail("invalid API fence accepted a pause mutation")


def test_pause_commit_holds_the_fence_until_database_commit(tmp_path, monkeypatch):
    path = tmp_path / "pause.lock"
    path.touch(mode=0o444)
    enable_api_fence(path, monkeypatch)
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        commit = db.commit

        def blocked_admission():
            with path.open() as handle:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            commit()

        monkeypatch.setattr(db, "commit", blocked_admission)
        assert control.set_agents_paused(db, True, actor="founder", reason="test")["paused"]
    engine.dispose()
