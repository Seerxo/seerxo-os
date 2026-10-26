"""agent execution control state

Revision ID: c304f9d92b7a
Revises: f1b2e6514e1e
Create Date: 2026-09-30 16:45:00+03:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c304f9d92b7a"
down_revision: str | None = "f1b2e6514e1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "control_state",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(length=120), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("control_state")
