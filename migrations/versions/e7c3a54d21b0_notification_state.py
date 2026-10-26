"""Persist incident-aware notification decisions and audit reasons.

Revision ID: e7c3a54d21b0
Revises: d4e8c2a7b9f1
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a54d21b0"
down_revision: str | None = "d4e8c2a7b9f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("notify_reason", sa.String(length=200), nullable=True))
    op.create_table(
        "notification_states",
        sa.Column("scope_key", sa.String(length=200), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=True),
        sa.Column("delivery_status", sa.String(length=20), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("notification_states")
    op.drop_column("events", "notify_reason")
