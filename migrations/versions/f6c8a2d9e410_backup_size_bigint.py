"""Allow backup sizes above 2 GiB without losing their integrity records.

Revision ID: f6c8a2d9e410
Revises: e7c3a54d21b0
"""
import sqlalchemy as sa
from alembic import op

revision = "f6c8a2d9e410"
down_revision = "e7c3a54d21b0"
branch_labels = None
depends_on = None


def upgrade():
    # The rewrite holds ACCESS EXCLUSIVE on backup_runs: give up instead of queueing behind a long transaction.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.alter_column("backup_runs", "size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger())


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM backup_runs WHERE size_bytes > 2147483647 OR size_bytes < -2147483648"
    )).scalar_one()
    if count:
        raise RuntimeError("backup sizes exceed integer range; downgrade would lose data")
    op.alter_column("backup_runs", "size_bytes", existing_type=sa.BigInteger(), type_=sa.Integer())
