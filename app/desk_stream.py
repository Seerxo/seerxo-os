"""Server-pushed change notices for the Founder Desk: GET /desk/stream (Server-Sent Events).

The stream is a notification, never a source of truth. It announces that committed state changed; the Desk then
re-reads the authoritative /desk/state. Its signal is the committed tail of rows every state change already writes
in the same transaction: audit_log (task status, claims, assignments, approvals, incidents), events (agent runs,
CI, backups, alerts), agent_runs, tasks.updated_at and control_state (pause, worker heartbeat, host metrics). Rows are
visible only after their transaction commits, so a notice can never announce an uncommitted change.

Cursor: per append-only table, the newest id plus the number of rows in the newest WINDOW ids. PostgreSQL assigns ids
at insert, not at commit, so a transaction holding a lower id can commit after a higher one was already seen: the max
alone would miss it, the count over the window does not. The cursor is sent as the SSE id, so a reconnect with
Last-Event-ID resumes; any difference (including a missed change) yields one notice and the Desk resynchronizes.

No model call, no write, no new infrastructure: a few indexed aggregate reads per poll on one connection.
ponytail: per-connection DB poll (POLL_S) with a small connection cap; a shared poller or LISTEN/NOTIFY if the Desk
ever has many concurrent viewers.
"""
import asyncio
import json
import time

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select

from app import models
from app.db import SessionLocal

POLL_S = 0.5           # how often committed state is checked; bounds notice latency
HEARTBEAT_S = 15       # a named event the Desk uses to tell a live stream from a dead one (proxies idle out at ~100 s)
MAX_S = 30 * 60        # a stream is recycled after this; the browser reconnects with Last-Event-ID
MAX_STREAMS = 8        # one founder, a few devices; more is refused rather than queued
WINDOW = 1000
_open = 0


def cursor(db) -> str:
    """A compact fingerprint of committed state; equal cursors mean nothing the Desk shows has changed."""
    def tail(model) -> str:
        top = db.scalar(select(func.max(model.id))) or 0
        n = db.scalar(select(func.count()).select_from(model).where(model.id > top - WINDOW))
        return f"{top}.{n}"

    def stamp(column) -> str:
        at = db.scalar(select(func.max(column)))
        return f"{at.timestamp():.6f}" if at else "0"

    return "-".join((
        "a" + tail(models.AuditLog), "e" + tail(models.Event), "r" + tail(models.AgentRun),
        "t" + stamp(models.Task.updated_at), "c" + stamp(models.ControlState.updated_at)))


def _read() -> str:
    with SessionLocal() as db:
        return cursor(db)


def _frame(event: str, data: dict, id_: str | None = None) -> str:
    return (f"id: {id_}\n" if id_ else "") + f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def notices(request: Request, last_id: str | None, read=_read, sleep=asyncio.sleep, clock=time.monotonic):
    """The SSE body: one `change` per new cursor, a `heartbeat` when quiet, `unavailable` when the DB cannot be read
    (the Desk then shows the stream as degraded and resynchronizes on the next change)."""
    sent, started, quiet = last_id, clock(), 0.0
    yield "retry: 3000\n\n"
    while clock() - started < MAX_S and not await request.is_disconnected():
        try:
            now = await asyncio.to_thread(read)
        except Exception:
            yield _frame("unavailable", {"retry_s": 5})
            await sleep(5)
            continue
        if now != sent:
            sent, quiet = now, 0.0
            yield _frame("change", {"cursor": now}, now)
        elif quiet >= HEARTBEAT_S:
            quiet = 0.0
            yield _frame("heartbeat", {"cursor": now})
        await sleep(POLL_S)
        quiet += POLL_S


def response(request: Request) -> StreamingResponse | JSONResponse:
    global _open
    if _open >= MAX_STREAMS:
        return JSONResponse({"detail": "too many live Desk streams"}, status_code=503)

    async def body():
        global _open
        _open += 1
        try:
            async for chunk in notices(request, request.headers.get("last-event-id")):
                yield chunk
        finally:
            _open -= 1

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
