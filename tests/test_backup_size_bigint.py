"""backup_runs.size_bytes is BIGINT: the 2026-10-18 weekly renders archive (16,955,774,638 bytes) overflowed the
INTEGER column, so POST /backups failed and that backup's report was lost."""
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateColumn

from app import models

ROOT = Path(__file__).resolve().parents[1]
RENDERS_2026_10_18 = 16_955_774_638
_spec = importlib.util.spec_from_file_location(
    "backup_size_bigint", ROOT / "migrations/versions/f6c8a2d9e410_backup_size_bigint.py")
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def test_model_column_is_bigint():
    sql = str(CreateColumn(models.BackupRun.__table__.c.size_bytes).compile(dialect=postgresql.dialect()))
    assert sql == "size_bytes BIGINT"


def test_upgrade_changes_only_that_column():
    out = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": out})
    with Operations.context(ctx):
        migration.upgrade()
    assert out.getvalue().split() == "SET LOCAL lock_timeout = '5s'; ALTER TABLE backup_runs ALTER COLUMN size_bytes TYPE BIGINT;".split()
    assert migration.down_revision == "e7c3a54d21b0"


# ---- PostgreSQL, disposable database only ----------------------------------------------------------------

def test_migration_on_postgres_keeps_rows_and_refuses_a_lossy_downgrade():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)

    def reset():
        models.Base.metadata.drop_all(engine)
        with engine.begin() as c:
            c.execute(text("drop table if exists alembic_version"))
            c.execute(text("drop type if exists experiment_status"))

    def alembic(*args, check=True):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, check=check,
                              env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True)

    def column_type():
        return next(c["type"] for c in inspect(engine).get_columns("backup_runs") if c["name"] == "size_bytes")

    def add(size):
        with engine.begin() as c:
            c.execute(text("insert into backup_runs (target, size_bytes, uploaded, integrity_verified, ok, started_at)"
                           " values ('renders', :s, true, false, true, now())"), {"s": size})

    reset()
    try:
        alembic("upgrade", "e7c3a54d21b0")
        add(1_048_576)                                        # an existing row survives the type change
        with pytest.raises(Exception, match="out of range"):  # the production failure, reproduced
            add(RENDERS_2026_10_18)
        alembic("upgrade", "head")
        assert isinstance(column_type(), postgresql.BIGINT)
        add(RENDERS_2026_10_18)
        with engine.connect() as c:
            assert sorted(r[0] for r in c.execute(text("select size_bytes from backup_runs"))) == [
                1_048_576, RENDERS_2026_10_18]

        refused = alembic("downgrade", "e7c3a54d21b0", check=False)
        assert refused.returncode != 0 and "would lose data" in refused.stderr
        assert isinstance(column_type(), postgresql.BIGINT)
        with engine.connect() as c:
            assert c.execute(text("select count(*) from backup_runs")).scalar() == 2

        with engine.begin() as c:
            c.execute(text("delete from backup_runs where size_bytes > 2147483647"))
        alembic("downgrade", "e7c3a54d21b0")                  # only small rows: narrowing is lossless
        assert isinstance(column_type(), postgresql.INTEGER)
        alembic("upgrade", "head")
        assert alembic("current").stdout.split()[0] == alembic("heads").stdout.split()[0]
    finally:
        reset()
        engine.dispose()
