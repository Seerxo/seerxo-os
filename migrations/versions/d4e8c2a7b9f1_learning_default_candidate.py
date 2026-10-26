"""Fail closed for newly created learning rows.

Existing APPROVED rows are historical records and are deliberately not rewritten.

Revision ID: d4e8c2a7b9f1
Revises: b4f8a2d6c1e9
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8c2a7b9f1"
down_revision: str | None = "b4f8a2d6c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


STATUS = sa.Enum("CANDIDATE", "APPROVED", name="learning_status")


def upgrade() -> None:
    op.alter_column("learnings", "status", existing_type=STATUS, server_default="CANDIDATE")


def downgrade() -> None:
    op.alter_column("learnings", "status", existing_type=STATUS, server_default="APPROVED")
