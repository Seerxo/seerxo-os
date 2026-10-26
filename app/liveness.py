"""Hang exit for long-running loops (L1 self-healing).

docker `restart: unless-stopped` restarts a process that exits, never one that is alive but stuck. A daemon
thread reads the loop's last progress time; once it is older than the limit the process exits and docker
restarts it. Work the process held is not lost: stale-run recovery picks it up on the next start.
"""
import logging
import os
import threading
import time

log = logging.getLogger("liveness")
EXIT_CODE = 75
EVERY_S = 30.0
_exit = os._exit          # hard exit: a stuck main thread must not be able to block it


def watch(progress, *, limit_s: float, every_s: float | None = None, clock=time.monotonic, sleep=time.sleep,
          exit=None) -> None:
    """Exit once the last progress (progress() -> clock time) is limit_s old. A probe that raises is no
    progress; the watcher itself never dies."""
    last = clock()
    while True:
        sleep(EVERY_S if every_s is None else every_s)
        try:
            last = max(last, progress())
        except Exception:
            pass
        if clock() - last >= limit_s:
            log.error("no progress for %ss (limit %ss): exiting so the container restarts",
                      round(clock() - last), limit_s)
            (exit or _exit)(EXIT_CODE)
            return


def start(progress, *, limit_s: float) -> threading.Thread:
    t = threading.Thread(target=watch, args=(progress,), kwargs={"limit_s": limit_s}, daemon=True,
                         name="liveness")
    t.start()
    return t
