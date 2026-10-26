"""learnings.status: a promoted notebook note is a CANDIDATE until reviewed (§53A)

Context building served every learning to agents, including the candidates notebook
compaction promotes, which only their title marked as unreviewed. Existing rows become
APPROVED (people recorded them), except compaction's own "Candidate rule (" rows, which
were never reviewed and become CANDIDATE.

Revision ID: c7d2e8a4f1b3
Revises: a3c9e1f4b6d8
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d2e8a4f1b3"
down_revision: str | None = "a3c9e1f4b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = sa.Enum("CANDIDATE", "APPROVED", name="learning_status")


def upgrade() -> None:
    STATUS.create(op.get_bind(), checkfirst=True)
    op.add_column("learnings", sa.Column("status", STATUS, nullable=False, server_default="APPROVED"))
    op.execute("UPDATE learnings SET status = 'CANDIDATE' WHERE title LIKE 'Candidate rule (%'")


def downgrade() -> None:
    op.drop_column("learnings", "status")
    STATUS.drop(op.get_bind(), checkfirst=True)
