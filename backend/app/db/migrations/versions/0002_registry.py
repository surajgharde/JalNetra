"""water_bodies and zones

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "water_bodies",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("district", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column("area_km2", sa.Float(), nullable=False),
        sa.Column("mgrs_tiles", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("tier IN (1, 2, 3)", name=op.f("ck_water_bodies_tier_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_water_bodies")),
    )
    op.create_index(op.f("ix_water_bodies_district"), "water_bodies", ["district"])
    op.create_index(op.f("ix_water_bodies_tier"), "water_bodies", ["tier"])
    op.create_index(
        "idx_water_bodies_geom", "water_bodies", ["geom"], postgresql_using="gist"
    )

    op.create_table(
        "zones",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column("area_km2", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_zones_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_zones")),
    )
    op.create_index(op.f("ix_zones_water_body_id"), "zones", ["water_body_id"])
    op.create_index("idx_zones_geom", "zones", ["geom"], postgresql_using="gist")


def downgrade() -> None:
    op.drop_index("idx_zones_geom", table_name="zones")
    op.drop_index(op.f("ix_zones_water_body_id"), table_name="zones")
    op.drop_table("zones")
    op.drop_index("idx_water_bodies_geom", table_name="water_bodies")
    op.drop_index(op.f("ix_water_bodies_tier"), table_name="water_bodies")
    op.drop_index(op.f("ix_water_bodies_district"), table_name="water_bodies")
    op.drop_table("water_bodies")
