"""GET /desk/stream on real PostgreSQL (READ COMMITTED): a notice never announces uncommitted work, and a lower id that
commits after a higher one is still announced. Requires EXTERNAL_RELEASE_TEST_DATABASE_URL (disposable database)."""
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import desk_stream, models

URL = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")


@pytest.fixture
def engine():
    if not URL:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(URL).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    schema = f"stream_{uuid.uuid4().hex[:12]}"
    admin = create_engine(URL)
    with admin.begin() as c:
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    eng = create_engine(make_url(URL).update_query_dict({"options": f"-csearch_path={schema}"}))
    models.Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.dispose()


def _event(db):
    db.add(models.Event(event_type="TASK_STARTED", source="company-worker", payload={}))
    db.flush()


def test_uncommitted_work_is_never_announced_and_its_commit_is(engine):
    with Session(engine) as writer, Session(engine) as reader:
        before = desk_stream.cursor(reader)
        reader.rollback()
        _event(writer)                                   # inserted, not committed
        assert desk_stream.cursor(reader) == before
        reader.rollback()
        writer.commit()
        assert desk_stream.cursor(reader) != before


def test_a_lower_id_committing_after_a_higher_one_is_announced(engine):
    with Session(engine) as slow, Session(engine) as fast, Session(engine) as reader:
        _event(slow)                                     # takes the lower id, commits last
        _event(fast)
        fast.commit()
        seen = desk_stream.cursor(reader)
        reader.rollback()
        slow.commit()
        assert desk_stream.cursor(reader) != seen        # max(id) did not move; the windowed count did
