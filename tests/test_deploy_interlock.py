"""The deploy interlock query runs against PostgreSQL's real JSON column."""
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import enums, models


DEPLOY = Path(__file__).parents[1] / "scripts" / "deploy-company-os.sh"
PREDICATE = "status = 'STARTED' and tool_calls->>'stage_c' = 'true'"
QUERY = text(f"select id from agent_runs where {PREDICATE} order by id")


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_started_stage_c_query_uses_postgres_json_without_casting(engine):
    assert PREDICATE in DEPLOY.read_text()
    with Session(engine) as db:
        db.execute(delete(models.AgentRun))
        rows = [
            models.AgentRun(status=enums.AgentRunStatus.STARTED, tool_calls={"stage_c": True}),
            models.AgentRun(status=enums.AgentRunStatus.STARTED, tool_calls={"stage_c": False}),
            models.AgentRun(status=enums.AgentRunStatus.STARTED, tool_calls=None),
            models.AgentRun(status=enums.AgentRunStatus.STARTED, tool_calls={"other": True}),
            models.AgentRun(status=enums.AgentRunStatus.SUCCEEDED, tool_calls={"stage_c": True}),
            models.AgentRun(status=enums.AgentRunStatus.FAILED, tool_calls={"stage_c": True}),
            models.AgentRun(status=enums.AgentRunStatus.STARTED, tool_calls={"stage_b": True}),
        ]
        db.add_all(rows); db.commit()
        assert db.execute(QUERY).scalars().all() == [rows[0].id]
