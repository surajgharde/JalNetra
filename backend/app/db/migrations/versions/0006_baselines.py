"""baselines, rainfall and the indicator_weekly continuous aggregate

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "baselines",
        sa.Column("zone_id", sa.Text(), nullable=False),
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("doy_window", sa.SmallInteger(), nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("window_days", sa.SmallInteger(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=True),
        sa.Column("std", sa.Float(), nullable=True),
        sa.Column("p10", sa.Float(), nullable=True),
        sa.Column("p90", sa.Float(), nullable=True),
        sa.Column("n_samples", sa.Integer(), nullable=False),
        sa.Column("n_years", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("history_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("history_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("history_days", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("doy_window BETWEEN 1 AND 366", name=op.f("ck_baselines_doy_range")),
        sa.CheckConstraint("status IN ('usable', 'building')", name=op.f("ck_baselines_status")),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_baselines_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name=op.f("fk_baselines_zone_id_zones"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("zone_id", "indicator", "doy_window", name=op.f("pk_baselines")),
    )
    op.create_index(op.f("ix_baselines_water_body_id"), "baselines", ["water_body_id"])

    op.create_table(
        "rainfall",
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("mm_24h", sa.REAL(), nullable=False),
        sa.Column("mm_72h", sa.REAL(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_rainfall_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("water_body_id", "date", name=op.f("pk_rainfall")),
    )
    op.create_index("ix_rainfall_date", "rainfall", [sa.text("date DESC")])

    # Weekly zone-indicator means for the frontend series endpoint (S9). A
    # continuous aggregate cannot be created inside a transaction, hence the
    # autocommit block. Weeks are bucketed from Monday 2017-01-02, the first
    # full week of the Sentinel-2 L2A archive, so buckets are stable across rebuilds.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE MATERIALIZED VIEW indicator_weekly
            WITH (timescaledb.continuous) AS
            SELECT
                time_bucket(INTERVAL '7 days', observed_at, TIMESTAMPTZ '2017-01-02') AS week,
                zone_id,
                water_body_id,
                indicator,
                avg(mean)             AS mean,
                avg(p90)              AS p90,
                max(p90)              AS p90_max,
                min(mean)             AS mean_min,
                max(mean)             AS mean_max,
                avg(valid_pixel_pct)  AS valid_pixel_pct,
                count(*)              AS n_obs
            FROM indicator_observations
            GROUP BY week, zone_id, water_body_id, indicator
            WITH NO DATA
            """
        )
        # Refresh the last 90 days hourly; older buckets are refreshed by the
        # baseline rebuild (see app.services.l07_baseline.service.refresh_weekly).
        op.execute(
            """
            SELECT add_continuous_aggregate_policy(
                'indicator_weekly',
                start_offset => INTERVAL '90 days',
                end_offset   => INTERVAL '1 hour',
                schedule_interval => INTERVAL '1 hour',
                if_not_exists => TRUE
            )
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS indicator_weekly CASCADE")
    op.drop_table("rainfall")
    op.drop_table("baselines")
