"""L1 self-healing: docker `restart: unless-stopped` restarts a process that EXITS, never one that is alive but
stuck. A worker or reasoner that stops making progress must exit so docker restarts it; the work it held is then
picked up by the normal stale-run recovery (tests/test_worker_crash_recovery_postgres.py::
test_sigkill_sweep_then_recovery proves nothing is lost when the process dies mid-run)."""
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app import liveness, reasoner, worker


class Exited(Exception):
    pass


class Done(BaseException):          # the watcher swallows Exception from a probe by design
    pass


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def exit_(code):
    raise Exited(code)


def test_a_stuck_loop_exits_once_progress_is_older_than_the_limit():
    clock, seen = Clock(), []

    def progress():
        seen.append(clock.t)
        return 0.0                                    # progress stopped at t=0
    with pytest.raises(Exited) as e:
        liveness.watch(progress, limit_s=100, every_s=30, clock=clock, sleep=clock.sleep, exit=exit_)
    assert e.value.args == (liveness.EXIT_CODE,) and clock.t == 120 and seen == [30, 60, 90, 120]


def test_a_loop_that_keeps_making_progress_is_never_killed():
    clock, checks = Clock(), []

    def progress():
        checks.append(clock.t)
        if len(checks) == 50:
            raise Done                                # end the test after 50 checks
        return clock.t - 10
    with pytest.raises(Done):
        liveness.watch(progress, limit_s=100, every_s=30, clock=clock, sleep=clock.sleep, exit=exit_)


def test_a_probe_that_raises_counts_as_no_progress_and_never_kills_the_watcher():
    clock = Clock()

    def progress():
        raise OSError("db gone")
    with pytest.raises(Exited):
        liveness.watch(progress, limit_s=60, every_s=30, clock=clock, sleep=clock.sleep, exit=exit_)
    assert clock.t == 60


def test_the_worker_exits_when_a_tick_hangs(monkeypatch):
    stuck, exits = threading.Event(), []
    monkeypatch.setattr(worker, "run_once", lambda db: stuck.wait(5) or {})
    monkeypatch.setattr(worker, "HANG_EXIT_S", 0.2)
    monkeypatch.setattr(worker.liveness, "EVERY_S", 0.05)

    def fake_exit(code):
        exits.append(code)
        stuck.set()                                   # unblock the tick so the test ends
    monkeypatch.setattr(worker.liveness, "_exit", fake_exit)
    monkeypatch.setattr("sys.argv", ["company-worker", "--once"])
    assert worker.main() == 0
    assert exits == [liveness.EXIT_CODE]


def test_the_hang_limit_outlasts_the_longest_legitimate_tick():
    # A Stage C attempt may hold one tick for up to an hour (worker.recover_stale run_cutoff).
    assert worker.HANG_EXIT_S > 3600 and worker.HANG_EXIT_S > worker.STALE_AFTER.total_seconds()


def test_the_reasoner_answers_its_liveness_probe_without_touching_a_provider():
    class Boom:
        def health_check(self):
            raise AssertionError("liveness must not run a CLI")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), reasoner.handler({"claude": Boom()}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(url + "/live", timeout=5) as r:
            assert r.status == 200
        clock = Clock()
        progress = reasoner.live_progress(url, clock=clock)
        assert progress() == 0.0
        clock.t = 500
        assert progress() == 500                      # a served probe is progress
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_reasoner_probe_never_goes_through_its_egress_proxy(monkeypatch):
    # Production sets HTTP(S)_PROXY to company-reasoner-egress with an empty NO_PROXY: the self-probe went to the
    # proxy, got 403 Filtered, and the reasoner restarted every 300 s.
    srv = ThreadingHTTPServer(("127.0.0.1", 0), reasoner.handler({}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")      # nothing listens: any proxied call fails
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("NO_PROXY", "")
        clock = Clock()
        progress = reasoner.live_progress(f"http://127.0.0.1:{srv.server_address[1]}", clock=clock)
        clock.t = 500
        assert progress() == 500
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_reasoner_that_stops_serving_exits():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), reasoner.handler({}))
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    srv.server_close()                                # bound once, now nothing answers
    clock = Clock()
    with pytest.raises(Exited):
        liveness.watch(reasoner.live_progress(url, clock=clock), limit_s=90, every_s=30, clock=clock,
                       sleep=clock.sleep, exit=exit_)
    assert clock.t == 90
