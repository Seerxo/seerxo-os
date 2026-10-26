"""goals.metric_selector: bind a goal to the metric observation that measures it

Additive and nullable: existing goals keep working and simply report no progress.

Revision ID: e5a1c3b7d9f2
Revises: d2f7a91c4e30
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1c3b7d9f2"
down_revision: str | None = "d2f7a91c4e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("goals", sa.Column("metric_selector", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("goals", "metric_selector")
