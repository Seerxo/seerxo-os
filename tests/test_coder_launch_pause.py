"""Founder pause and cancellation at the privileged launch boundary; no Docker is run."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import time

import pytest

from tests.test_coder_launch import CFG, L, _job, _load


def fence(mod, tmp_path, monkeypatch):
    path = tmp_path / "pause.lock"
    path.touch()
    path.chmod(0o444)
    monkeypatch.setattr(mod, "PAUSE_FENCE", path)
    monkeypatch.setattr(mod, "_FENCE_UID", os.getuid())
    return path


@pytest.mark.parametrize("reason", ["founder pause is active", "pause authority is unavailable"])
def test_a_queued_request_never_starts_without_pause_authority(L, monkeypatch, reason):
    job = _job(L)
    calls = []

    @contextmanager
    def denied():
        raise L.Refused(reason)
        yield

    monkeypatch.setattr(L, "pause_guard", denied, raising=False)
    result = L.handle("TASK-0101", CFG, runner=lambda *a: (calls.append(a) or {"exit": 0, "timed_out": False}))
    assert calls == []
    assert reason in result["refused"]
    assert not (job / "done/started.json").exists()


def test_the_pause_query_is_valid_python():
    compile(_load().PAUSE_QUERY, "pause-query", "exec")


@pytest.mark.parametrize("script,allowed", [
    ('print("READY", flush=True); import sys; '
     'assert sys.stdin.readline() == "START\\n"; '
     'print("ALLOW", flush=True); sys.stdin.readline()', True),
    ('raise SystemExit(2)', False),
    ('print("PAUSED", flush=True)', False),
    ('print("READY", flush=True)', False),
])
def test_pause_authority_protocol_fails_closed(tmp_path, monkeypatch, script, allowed):
    import subprocess
    import sys
    mod = _load()
    fence(mod, tmp_path, monkeypatch)
    popen, calls = subprocess.Popen, []

    def helper(argv, **kwargs):
        calls.append(argv)
        assert argv[:6] == ["docker", "exec", "-i", "--user=10001:10001", "company-api", "python"]
        assert "env" not in kwargs
        return popen([sys.executable, "-u", "-c", script], **kwargs)

    monkeypatch.setattr(mod.subprocess, "Popen", helper)
    if allowed:
        with mod.pause_guard() as authorize:
            authorize()
    else:
        with pytest.raises((mod.Refused, BrokenPipeError)):
            with mod.pause_guard() as authorize:
                authorize()
    assert len(calls) == 1


def test_pause_permission_is_released_before_the_admitted_job_runs(L, monkeypatch):
    job = _job(L)
    events = []

    @contextmanager
    def guard():
        events.append("locked")
        try:
            yield lambda: events.append("authorized")
        finally:
            events.append("released")

    def coder(argv, *args):
        mount = Path(argv[argv.index("--mount") + 1].split(",")[1].split("=", 1)[1])
        assert (mount.parent / "ack/allowed").is_file()
        assert (job / "done/started.json").is_file()
        events.append("coder")
        return {"exit": 0, "timed_out": False}

    monkeypatch.setattr(L, "pause_guard", guard)
    result = L.handle("TASK-0101", CFG, runner=coder)
    assert result["exit"] == 0
    assert events == ["locked", "authorized", "released", "coder"]


def test_the_container_wrapper_waits_for_root_permission_before_executing(tmp_path):
    mod = _load()
    ack = tmp_path / "ack"
    ack.mkdir()
    executed = tmp_path / "executed"
    wrapper = mod.START_WRAPPER.replace("/run/company-coder", str(ack))
    proc = subprocess.Popen(["sh", "-c", wrapper, "coder", "touch", str(executed)])
    try:
        deadline = time.monotonic() + 2
        while not (ack / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (ack / "started").exists()
        assert not executed.exists()
        (ack / "allowed").touch()
        assert proc.wait(timeout=2) == 0
        assert executed.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_a_wrapper_without_root_permission_exits_without_running_code(tmp_path):
    mod = _load()
    ack = tmp_path / "ack"
    ack.mkdir()
    executed = tmp_path / "executed"
    wrapper = mod.START_WRAPPER.replace("/run/company-coder", str(ack)).replace('"$n" -lt 600', '"$n" -lt 2')
    result = subprocess.run(["sh", "-c", wrapper, "coder", "touch", str(executed)], timeout=2)
    assert result.returncode == 124
    assert not executed.exists()


def test_a_pause_authority_lost_during_container_start_never_grants_execution(L, monkeypatch):
    job = _job(L)
    calls = []

    def authorize():
        raise L.Refused("pause authority was lost")

    @contextmanager
    def guard():
        yield authorize

    monkeypatch.setattr(L, "pause_guard", guard, raising=False)
    result = L.handle("TASK-0101", CFG, runner=lambda *a: (calls.append(a) or {"exit": 0, "timed_out": False}))
    assert calls == []
    assert "pause authority was lost" in result["refused"]
    assert not (job / "done/started.json").exists()


def test_cancellation_while_waiting_for_pause_authority_never_starts_the_coder(L, monkeypatch):
    job = _job(L)
    calls = []

    @contextmanager
    def guard():
        (job / "cancel.json").write_text("cancelled")
        yield lambda: None

    monkeypatch.setattr(L, "pause_guard", guard, raising=False)
    result = L.handle("TASK-0101", CFG, runner=lambda *a: (calls.append(a) or {"exit": 0, "timed_out": False}))
    assert calls == []
    assert "cancelled before coder start" in result["refused"]
    assert not (job / "done/started.json").exists()


def test_failed_permission_write_does_not_report_a_model_start(L, monkeypatch):
    job = _job(L)
    original_open = os.open
    calls = []

    def fail_permission(path, *args, **kwargs):
        if Path(path).name == "allowed":
            raise OSError("permission volume unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_permission)
    result = L.handle("TASK-0101", CFG, runner=lambda *a: calls.append(a))
    assert "permission volume unavailable" in result["refused"]
    assert calls == []
    assert not (job / "done/started.json").exists()


def test_a_marker_cannot_release_task_ownership_before_the_launcher_grants_permission(L, monkeypatch):
    from app import stage_c_runner
    import fcntl
    job = _job(L)
    starts, locks = [], []
    monkeypatch.setattr(stage_c_runner, "LAUNCH_LOCK_TIMEOUT", 0.05)

    def launch_pending(_):
        if not locks:
            locks.append(L.launch_lock(job))
            (job / "done").mkdir()
            (job / "done/started.json").touch()

    try:
        with pytest.raises(stage_c_runner.CoderStartStateUnknown):
            stage_c_runner._launch(job, {"kind": "code", "provider": "claude", "timeout_s": 10},
                                   0.2, 0.01, launch_pending, before_enqueue=lambda: None,
                                   after_start=starts.append)
        assert starts == []
    finally:
        for fd in locks:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
