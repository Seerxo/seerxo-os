"""metric observations: business measurements with provenance

Revision ID: d2f7a91c4e30
Revises: b8e41d0c7a52
Create Date: 2026-10-05 12:00:00+03:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d2f7a91c4e30"
down_revision: str | None = "b8e41d0c7a52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive only: a new table, no existing row is touched.
    op.create_table(
        "metric_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_unit_id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("metric", sa.String(length=80), nullable=False),
        sa.Column("window", sa.String(length=40), nullable=False),
        sa.Column("value", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["business_unit_id"], ["business_units.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "subject", "metric", "window", name="uq_metric_observation"),
    )
    op.create_index("ix_metric_observations_business_unit_id", "metric_observations",
                    ["business_unit_id"])
    op.create_index("ix_metric_observations_observed_at", "metric_observations", ["observed_at"])


def downgrade() -> None:
    op.drop_index("ix_metric_observations_observed_at", table_name="metric_observations")
    op.drop_index("ix_metric_observations_business_unit_id", table_name="metric_observations")
    op.drop_table("metric_observations")
