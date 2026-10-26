"""experiments: first-class lifecycle for "did this improvement work"

Additive: one new table, one new enum type and its indexes; no existing table is touched. The automatic
deploy refuses schema changes, so this needs an attended migration.

Safety: CHECK constraints keep invalid rows out; foreign keys are RESTRICT, so an observation, goal or
learning an experiment cites cannot be deleted from under it (the evidence JSON also snapshots the numbers).
DOWNGRADE DESTROYS EXPERIMENT ROWS. It therefore refuses while the table holds rows, unless
EXPERIMENTS_DOWNGRADE_CONFIRM_DATA_LOSS=yes is set after the rows were exported
(docs/EXPERIMENT_ENGINE.md, "Attended rollback").

Revision ID: b4f8a2d6c1e9
Revises: c7d2e8a4f1b3
"""
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4f8a2d6c1e9"
down_revision: str | None = "c7d2e8a4f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VALUES = ("PROPOSED", "READY", "RUNNING", "AWAITING_MEASUREMENT", "EVALUATED", "ACCEPTED", "REJECTED", "CANCELLED")
STATUS = postgresql.ENUM(*VALUES, name="experiment_status", create_type=False)


def _ts(name):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    postgresql.ENUM(*VALUES, name="experiment_status").create(op.get_bind(), checkfirst=True)
    op.create_table(
        "experiments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("goal_id", sa.Integer(), sa.ForeignKey("goals.id"), nullable=True),
        sa.Column("business_unit_id", sa.Integer(), sa.ForeignKey("business_units.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("metric_selector", sa.JSON(), nullable=False),
        sa.Column("expected_direction", sa.String(4), nullable=False),
        sa.Column("min_effect", sa.Numeric(18, 4), nullable=False),
        sa.Column("measurement_window_hours", sa.Integer(), nullable=False),
        sa.Column("intervention_ref", sa.String(300), nullable=True),
        sa.Column("baseline_observation_id", sa.Integer(), sa.ForeignKey("metric_observations.id"), nullable=True),
        sa.Column("result_observation_id", sa.Integer(), sa.ForeignKey("metric_observations.id"), nullable=True),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("approved_by", sa.String(100), nullable=True),
        _ts("ready_at"), _ts("started_at"), _ts("measure_after"), _ts("evaluated_at"),
        sa.Column("decided_by", sa.String(100), nullable=True),
        _ts("decided_at"),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("learning_id", sa.Integer(), sa.ForeignKey("learnings.id"), nullable=True),
        sa.Column("goal_selector_snapshot", sa.JSON(), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("expected_direction in ('UP', 'DOWN')", name="ck_experiments_direction"),
        sa.CheckConstraint("min_effect >= 0", name="ck_experiments_min_effect"),
        sa.CheckConstraint("measurement_window_hours > 0", name="ck_experiments_window_hours"),
    )
    op.create_index("ix_experiments_key", "experiments", ["key"], unique=True)
    op.create_index("ix_experiments_status", "experiments", ["status"])
    op.create_index("ix_experiments_goal_id", "experiments", ["goal_id"])
    # One open experiment per proposal: a repeated request cannot create a second one.
    op.create_index("uq_experiments_open_fingerprint", "experiments", ["fingerprint"], unique=True,
                    postgresql_where=sa.text("status in ('PROPOSED', 'READY', 'RUNNING', 'AWAITING_MEASUREMENT', 'EVALUATED')"))


def downgrade() -> None:
    rows = op.get_bind().execute(sa.text("select count(*) from experiments")).scalar()
    if rows and os.environ.get("EXPERIMENTS_DOWNGRADE_CONFIRM_DATA_LOSS") != "yes":
        raise RuntimeError(
            f"refusing to drop experiments: it holds {rows} row(s) and the downgrade would destroy them. "
            "Export them first, then rerun with EXPERIMENTS_DOWNGRADE_CONFIRM_DATA_LOSS=yes "
            "(docs/EXPERIMENT_ENGINE.md, 'Attended rollback').")
    op.drop_index("uq_experiments_open_fingerprint", table_name="experiments")
    op.drop_index("ix_experiments_goal_id", table_name="experiments")
    op.drop_index("ix_experiments_status", table_name="experiments")
    op.drop_index("ix_experiments_key", table_name="experiments")
    op.drop_table("experiments")
    postgresql.ENUM(name="experiment_status").drop(op.get_bind(), checkfirst=True)
