"""open work unique indexes: one open task per dedupe_key, one unresolved incident per scope

Revision ID: b8e41d0c7a52
Revises: c304f9d92b7a
Create Date: 2026-10-02 19:30:00+03:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b8e41d0c7a52"
down_revision: str | None = "c304f9d92b7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN = "('READY','QUEUED','IN_PROGRESS','WAITING_EXTERNAL','WAITING_APPROVAL','BLOCKED','REVIEW')"


def upgrade() -> None:
    # Existing duplicates would make the index fail half-way; stop with the evidence instead.
    # Nothing is deleted or rewritten here (§23): the founder resolves them, then re-runs.
    dupes = op.get_bind().execute(sa.text(
        f"SELECT dedupe_key, string_agg(key, ', ' ORDER BY key) FROM tasks "
        f"WHERE dedupe_key IS NOT NULL AND status IN {OPEN} "
        f"GROUP BY dedupe_key HAVING count(*) > 1")).all()
    if dupes:
        raise RuntimeError("Duplicate open tasks share a dedupe_key; resolve before upgrading: "
                           + "; ".join(f"{k}: {keys}" for k, keys in dupes))
    op.add_column("incidents", sa.Column("open_scope", sa.String(length=300), nullable=True))
    op.create_index("uq_tasks_open_dedupe", "tasks", ["dedupe_key"], unique=True,
                    postgresql_where=sa.text(f"dedupe_key IS NOT NULL AND status IN {OPEN}"))
    op.create_index("uq_incidents_open_scope", "incidents", ["open_scope"], unique=True,
                    postgresql_where=sa.text("open_scope IS NOT NULL AND status != 'RESOLVED'"))


def downgrade() -> None:
    op.drop_index("uq_incidents_open_scope", table_name="incidents")
    op.drop_index("uq_tasks_open_dedupe", table_name="tasks")
    op.drop_column("incidents", "open_scope")
