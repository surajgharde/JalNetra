"""scenes and scene_ingestions

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scenes",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("mgrs_tile", sa.Text(), nullable=False),
        sa.Column("sensed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("cloud_pct", sa.Float(), nullable=False),
        sa.Column("stac_href", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("usable", sa.Boolean(), nullable=False),
        sa.Column("assets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("epsg", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scenes")),
    )
    op.create_index(op.f("ix_scenes_mgrs_tile"), "scenes", ["mgrs_tile"])
    op.create_index(op.f("ix_scenes_sensed_at"), "scenes", ["sensed_at"])

    op.create_table(
        "scene_ingestions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=True),
        sa.Column("bytes_read", sa.Integer(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"], ["scenes.id"], name=op.f("fk_scene_ingestions_scene_id_scenes"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_scene_ingestions_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scene_ingestions")),
        sa.UniqueConstraint("water_body_id", "scene_id", name=op.f("uq_scene_ingestions_water_body_scene")),
    )
    op.create_index(op.f("ix_scene_ingestions_scene_id"), "scene_ingestions", ["scene_id"])
    op.create_index(op.f("ix_scene_ingestions_water_body_id"), "scene_ingestions", ["water_body_id"])


def downgrade() -> None:
    op.drop_table("scene_ingestions")
    op.drop_index(op.f("ix_scenes_sensed_at"), table_name="scenes")
    op.drop_index(op.f("ix_scenes_mgrs_tile"), table_name="scenes")
    op.drop_table("scenes")
