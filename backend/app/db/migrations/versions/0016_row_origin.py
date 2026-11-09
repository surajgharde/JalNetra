"""origin: real pipeline output vs simulated

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-31

Demos need anomalies on demand, which means rows that did not come from a
satellite pass. Those rows are indistinguishable from real ones once they are
in the tables -- same shape, same scores, same alerts -- so every table the
simulator writes to carries ``origin``:

    original   - produced by the real pipeline from a real Sentinel-2 scene
    simulation - produced by `python -m app.cli simulate-anomaly`

Existing rows are all ``original``, and that stays the default, so a row is
only ever marked simulated by the simulator itself. ``scenes.source`` also
gets ``'simulation'`` for synthetic scenes rather than a STAC provider name it
never came from.

This is the only record that a row is synthetic -- the API does not expose it
-- so keep it accurate, and use ``simulate-anomaly --list`` / ``--purge`` to
find and remove simulated data rather than deleting rows by hand.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("scenes", "anomaly_candidates", "candidate_scores", "alerts")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("origin", sa.Text(), nullable=False, server_default="original"),
        )
        # Partial index: simulated rows are the rare case and the only one ever
        # filtered on (listing and purging them).
        op.create_index(
            f"idx_{table}_simulated",
            table,
            ["origin"],
            postgresql_where=sa.text("origin <> 'original'"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"idx_{table}_simulated", table_name=table)
        op.drop_column(table, "origin")
