"""GET /desk/stream: a change notice for every committed change the Desk shows, nothing for uncommitted work, and an
honest heartbeat/unavailable signal. The stream is a notification; the Desk always re-reads /desk/state."""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import desk_stream, enums, models


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    return engine


def _task(db, key="TASK-0001"):
    t = models.Task(key=key, title="t", success_criteria="s", status=enums.TaskStatus.READY)
    db.add(t)
    db.commit()
    return t


def test_every_kind_of_change_the_desk_shows_moves_the_cursor():
    engine = _db()
    with Session(engine) as db:
        t = _task(db)
        seen = {desk_stream.cursor(db)}
        changes = [
            lambda: db.add(models.AuditLog(actor="x", action="task.status", entity_type="task", entity_key=t.key, task_id=t.id)),
            lambda: db.add(models.Event(event_type="TASK_STARTED", source="company-worker", payload={})),
            lambda: db.add(models.AgentRun(task_id=t.id, provider="claude", status=enums.AgentRunStatus.STARTED)),
            # A task row change without an audit row (SQLite's now() has 1 s resolution, so the time is explicit).
            lambda: (setattr(t, "status", enums.TaskStatus.IN_PROGRESS),
                     setattr(t, "updated_at", datetime(2099, 1, 1, tzinfo=timezone.utc))),
            lambda: db.add(models.ControlState(key="agents", value={"paused": True}, updated_by="founder")),
        ]
        for change in changes:
            change()
            db.commit()
            now = desk_stream.cursor(db)
            assert now not in seen, change
            seen.add(now)


def test_an_uncommitted_change_is_never_announced():
    engine = _db()
    with Session(engine) as writer, Session(engine) as reader:
        _task(writer)
        before = desk_stream.cursor(reader)
        writer.add(models.Event(event_type="X", source="s", payload={}))
        writer.flush()  # inserted, not committed
        reader.rollback()
        # StaticPool shares one SQLite connection, so the uncommitted row is visible here; the PostgreSQL case
        # (READ COMMITTED) is in test_desk_stream_postgres. This asserts the cursor itself has no side effects.
        writer.rollback()
        assert desk_stream.cursor(reader) == before


def test_a_late_commit_with_a_lower_id_still_moves_the_cursor():
    """Ids are assigned at insert, not commit: a lower id that commits after a higher one changes no max, but it
    changes the windowed count."""
    engine = _db()
    with Session(engine) as db:
        for i in (1, 2, 4):
            db.add(models.Event(id=i, event_type="X", source="s", payload={}))
        db.commit()
        before = desk_stream.cursor(db)
        db.add(models.Event(id=3, event_type="X", source="s", payload={}))  # the late one
        db.commit()
        assert desk_stream.cursor(db) != before


class _Request:
    def __init__(self, polls):
        self.polls = polls

    async def is_disconnected(self):
        self.polls -= 1
        return self.polls < 0


def _run(gen):
    async def collect():
        return [chunk async for chunk in gen]
    return asyncio.run(collect())


def test_the_stream_sends_one_change_per_new_cursor_then_heartbeats():
    cursors = iter(["c1", "c1", "c2"] + ["c2"] * 40)
    out = _run(desk_stream.notices(_Request(43), None, read=lambda: next(cursors), sleep=lambda s: asyncio.sleep(0)))
    changes = [c for c in out if c.startswith("id: ")]
    assert changes == ['id: c1\nevent: change\ndata: {"cursor":"c1"}\n\n', 'id: c2\nevent: change\ndata: {"cursor":"c2"}\n\n']
    assert any(c.startswith("event: heartbeat") for c in out)
    assert out[0] == "retry: 3000\n\n"


def test_a_reconnect_with_the_current_cursor_sends_no_spurious_change():
    out = _run(desk_stream.notices(_Request(3), "c1", read=lambda: "c1", sleep=lambda s: asyncio.sleep(0)))
    assert not any("event: change" in c for c in out)


def test_a_database_error_is_announced_never_silently_healthy():
    def broken():
        raise RuntimeError("db down")
    out = _run(desk_stream.notices(_Request(2), None, read=broken, sleep=lambda s: asyncio.sleep(0)))
    assert any(c.startswith("event: unavailable") for c in out) and not any("event: change" in c for c in out)


def test_streams_are_capped(monkeypatch):
    monkeypatch.setattr(desk_stream, "_open", desk_stream.MAX_STREAMS)
    assert desk_stream.response(_Request(0)).status_code == 503
