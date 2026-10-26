"""metric_observations keeps history: one row per reading, not one row per series

The old key (source, subject, metric, window) made a newer reading overwrite the older
one, so a channel snapshot taken every 6 h lost its history. The key now includes
observed_at. No row is touched on upgrade.

Revision ID: a3c9e1f4b6d8
Revises: e5a1c3b7d9f2
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e1f4b6d8"
down_revision: str | None = "e5a1c3b7d9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERIES = ["source", "subject", "metric", "window"]


def upgrade() -> None:
    op.drop_constraint("uq_metric_observation", "metric_observations", type_="unique")
    op.create_unique_constraint("uq_metric_observation_reading", "metric_observations",
                                [*SERIES, "observed_at"])


def downgrade() -> None:
    # Going back would need history deleted; stop with the evidence instead (§23).
    n = op.get_bind().execute(sa.text(
        'SELECT count(*) FROM (SELECT 1 FROM metric_observations GROUP BY source, subject, '
        'metric, "window" HAVING count(*) > 1) s')).scalar_one()
    if n:
        raise RuntimeError(f"{n} metric series have history; downgrading would require deleting it")
    op.drop_constraint("uq_metric_observation_reading", "metric_observations", type_="unique")
    op.create_unique_constraint("uq_metric_observation", "metric_observations", SERIES)
