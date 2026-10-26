"""Bounded non-root crash probe. No Docker, database, provider, or credential."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from app import stage_c_runner
from tests.test_coder_launch import CFG, L, _job


@pytest.mark.parametrize("crash_at", ["grant", "result-publication"])
def test_granted_execution_cannot_be_reported_as_not_started_after_launcher_death(L, tmp_path, monkeypatch, crash_at):
    job = _job(L)
    executed = tmp_path / "local-command-executed"
    starts, child, wrapper = [], None, None
    child_status = None
    monkeypatch.setattr(stage_c_runner, "START_ACK_TIMEOUT", 1.0)
    monkeypatch.setattr(stage_c_runner, "LAUNCH_LOCK_TIMEOUT", 0.05)

    def poll(_):
        nonlocal child, wrapper, child_status
        if child is None:
            child = os.fork()
            if child == 0:
                original_open, original_worker = os.open, L.as_worker
                granted = False

                def worker(*cmd, **kwargs):
                    original_worker(*cmd, **kwargs)
                    if granted and cmd[0] in ("rm", "mkdir") and str(job / "done") in cmd:
                        os._exit(73)
                    if cmd[0] == "mkdir" and Path(cmd[-1]).name == "ack":
                        deadline = time.monotonic() + 2
                        while not (Path(cmd[-1]) / "started").exists():
                            if time.monotonic() >= deadline:
                                os._exit(74)
                            time.sleep(0.01)

                def opened(path, *args, **kwargs):
                    nonlocal granted
                    result = original_open(path, *args, **kwargs)
                    if Path(path).name == "allowed":
                        granted = True
                        if crash_at == "grant":
                            os._exit(73)  # Crash at the first possible instruction after the grant.
                    return result

                L.as_worker, os.open = worker, opened
                try:
                    L.handle("TASK-0101", CFG, runner=lambda *a: {"exit": 0, "timed_out": False})
                finally:
                    os._exit(75)
        if wrapper is None:
            acks = list(L.STAGING.glob("*/ack"))
            if acks:
                command = L.START_WRAPPER.replace("/run/company-coder", str(acks[0]))
                wrapper = subprocess.Popen(["sh", "-c", command, "coder", "touch", str(executed)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wrapper is not None and child_status is None:
            # Exercise recovery after the injected death, rather than a live admission handoff.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pid, status = os.waitpid(child, os.WNOHANG)
                if pid:
                    child_status = status
                    break
                time.sleep(0.01)
            assert child_status is not None, "the injected launcher did not exit"
        time.sleep(0.01)

    try:
        with pytest.raises(TimeoutError):
            stage_c_runner._launch(job, {"kind": "code", "provider": "claude", "timeout_s": 10},
                                   3, 0.01, poll, before_enqueue=lambda: None,
                                   after_start=starts.append)
        assert child is not None and child_status == 73 << 8
        child = None
        assert wrapper is not None and wrapper.wait(timeout=2) == 0
        assert executed.exists()
        assert list(L.STAGING.glob("*/ack/allowed"))
        assert (job / "done/started.json").exists()
        print(json.dumps({"permission_granted": True, "local_command_executed": True,
                          "launcher_exit": 73, "after_start": starts}))
        assert starts == [True], "a granted command was accounted as never started"
    finally:
        if wrapper is not None and wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=2)
        if child is not None and child_status is None:
            os.kill(child, 9)
            os.waitpid(child, 0)
