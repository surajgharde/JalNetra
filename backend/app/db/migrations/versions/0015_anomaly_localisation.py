"""within-zone localisation of a flagged zone

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-31

The spatial detector contrasts each pixel against the *whole body's* water
median and attributes each cluster to the single zone holding most of its
pixels. A zone that deviated from its own seasonal baseline therefore often
holds no cluster at all -- the body-wide contrast is dominated by whichever
zone is worst -- and the alert fell back to the entire zone polygon. 41% of
active alerts were in that state, and a zone is a quarter of the lake, so
viewing one highlighted most of the water body.

A second pass now re-contrasts such a zone against its own water to localise
the deviation. It is display only and never votes, so no stored severity,
confidence or priority changes. These columns record its output and, via
``geom_kind``, which kind of shape ``geom``/``spatial_geom`` actually is:

    plume     - body-level DBSCAN cluster (what `spatial_flag` already means)
    localised - within-zone patch from the new pass
    zone      - no patch; the deviation is zone-wide and the shape is the zone

Existing rows are backfilled to plume/zone from the columns that already
distinguish the two; re-running L8 turns the eligible `zone` rows into
`localised` ones.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "anomaly_candidates",
        sa.Column("localisation", JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column("anomaly_candidates", sa.Column("localised_area_km2", sa.Float(), nullable=True))
    op.add_column(
        "anomaly_candidates",
        sa.Column("geom_kind", sa.Text(), nullable=False, server_default="zone"),
    )
    op.add_column(
        "alerts", sa.Column("geom_kind", sa.Text(), nullable=False, server_default="zone")
    )
    op.add_column("alerts", sa.Column("localised_area_km2", sa.Float(), nullable=True))

    # `spatial_geom` is non-null exactly when the body-wide pass clustered, so
    # the existing rows already tell plume from zone.
    op.execute(
        "UPDATE anomaly_candidates SET geom_kind = 'plume' WHERE spatial_geom IS NOT NULL"
    )
    # Alerts copy the kind from the observation their current geometry came from.
    op.execute(
        """
        UPDATE alerts a
           SET geom_kind = c.geom_kind
          FROM anomaly_candidates c
         WHERE c.id = a.latest_candidate_id
        """
    )


def downgrade() -> None:
    op.drop_column("alerts", "localised_area_km2")
    op.drop_column("alerts", "geom_kind")
    op.drop_column("anomaly_candidates", "geom_kind")
    op.drop_column("anomaly_candidates", "localised_area_km2")
    op.drop_column("anomaly_candidates", "localisation")
