"""water_masks and raster_chips

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "water_masks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("sensed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("aoi_pixels", sa.Integer(), nullable=True),
        sa.Column("valid_pixel_pct", sa.Float(), nullable=True),
        sa.Column("cloud_pixel_pct", sa.Float(), nullable=True),
        sa.Column("usable", sa.Boolean(), nullable=False),
        sa.Column("mndwi_threshold", sa.Float(), nullable=True),
        sa.Column("threshold_method", sa.Text(), nullable=True),
        sa.Column("water_pixels", sa.Integer(), nullable=True),
        sa.Column("water_extent_km2", sa.Float(), nullable=True),
        sa.Column("water_fraction_pct", sa.Float(), nullable=True),
        sa.Column("n_components", sa.Integer(), nullable=True),
        sa.Column("chip_key", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"], ["scenes.id"], name=op.f("fk_water_masks_scene_id_scenes"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_water_masks_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_water_masks")),
        sa.UniqueConstraint("water_body_id", "scene_id", name=op.f("uq_water_masks_water_body_scene")),
    )
    op.create_index(op.f("ix_water_masks_scene_id"), "water_masks", ["scene_id"])
    op.create_index(op.f("ix_water_masks_sensed_at"), "water_masks", ["sensed_at"])
    op.create_index(op.f("ix_water_masks_water_body_id"), "water_masks", ["water_body_id"])

    op.create_table(
        "raster_chips",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("zone_id", sa.Text(), nullable=True),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("bounds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("crs", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"], ["scenes.id"], name=op.f("fk_raster_chips_scene_id_scenes"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_raster_chips_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name=op.f("fk_raster_chips_zone_id_zones"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raster_chips")),
        sa.UniqueConstraint("s3_key", name=op.f("uq_raster_chips_s3_key")),
    )
    op.create_index(op.f("ix_raster_chips_scene_id"), "raster_chips", ["scene_id"])
    op.create_index(op.f("ix_raster_chips_water_body_id"), "raster_chips", ["water_body_id"])
    op.create_index(op.f("ix_raster_chips_zone_id"), "raster_chips", ["zone_id"])


def downgrade() -> None:
    op.drop_table("raster_chips")
    op.drop_table("water_masks")
