"""indicator_observations hypertable and indicator_runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "indicator_observations",
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("zone_id", sa.Text(), nullable=False),
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=True),
        sa.Column("p90", sa.Float(), nullable=True),
        sa.Column("std", sa.Float(), nullable=True),
        sa.Column("n_pixels", sa.Integer(), nullable=False),
        sa.Column("valid_pixel_pct", sa.REAL(), nullable=False),
        sa.Column("water_fraction_pct", sa.REAL(), nullable=False),
        sa.Column("clipped_pct", sa.REAL(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scenes.id"],
            name=op.f("fk_indicator_observations_scene_id_scenes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_indicator_observations_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["zones.id"],
            name=op.f("fk_indicator_observations_zone_id_zones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "observed_at", "zone_id", "indicator", name=op.f("pk_indicator_observations")
        ),
    )
    # A zone sees one scene every 2-5 days, so 7-day default chunks would be tiny;
    # quarterly chunks keep the chunk count manageable at state scale.
    op.execute(
        "SELECT create_hypertable('indicator_observations', 'observed_at', "
        "chunk_time_interval => INTERVAL '3 months', if_not_exists => TRUE)"
    )
    op.create_index(
        "ix_indicator_observations_zone_indicator_time",
        "indicator_observations",
        ["zone_id", "indicator", sa.text("observed_at DESC")],
    )
    op.create_index(
        op.f("ix_indicator_observations_scene_id"), "indicator_observations", ["scene_id"]
    )
    op.create_index(
        op.f("ix_indicator_observations_water_body_id"), "indicator_observations", ["water_body_id"]
    )

    op.create_table(
        "indicator_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("sensed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("n_zones", sa.Integer(), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=True),
        sa.Column("rejected", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("chips", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("boa_offset", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"], ["scenes.id"], name=op.f("fk_indicator_runs_scene_id_scenes"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_indicator_runs_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indicator_runs")),
        sa.UniqueConstraint(
            "water_body_id", "scene_id", name=op.f("uq_indicator_runs_water_body_scene")
        ),
    )
    op.create_index(op.f("ix_indicator_runs_scene_id"), "indicator_runs", ["scene_id"])
    op.create_index(op.f("ix_indicator_runs_sensed_at"), "indicator_runs", ["sensed_at"])
    op.create_index(op.f("ix_indicator_runs_water_body_id"), "indicator_runs", ["water_body_id"])


def downgrade() -> None:
    op.drop_table("indicator_runs")
    op.drop_table("indicator_observations")  # drops the hypertable and its chunks
