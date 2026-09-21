"""anomaly_candidates and anomaly_runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-21

"""

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "anomaly_candidates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("zone_id", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indicator_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("temporal", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("temporal_flag", sa.Boolean(), nullable=False),
        sa.Column("max_abs_z", sa.Float(), nullable=True),
        sa.Column("anomalous_indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("spatial", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("spatial_flag", sa.Boolean(), nullable=False),
        sa.Column(
            "spatial_geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("affected_area_km2", sa.Float(), nullable=True),
        sa.Column("multivariate_score", sa.Float(), nullable=True),
        sa.Column("multivariate_flag", sa.Boolean(), nullable=False),
        sa.Column("multivariate", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("votes", sa.SmallInteger(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("natural_cause_likely", sa.Boolean(), nullable=False),
        sa.Column("rainfall_gate", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rainfall_72h", sa.REAL(), nullable=True),
        sa.Column("baseline_status", sa.Text(), nullable=False),
        sa.Column("alertable", sa.Boolean(), nullable=False),
        sa.Column("suppressed_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN ('low', 'medium', 'high')",
            name=op.f("ck_anomaly_candidates_severity"),
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scenes.id"],
            name=op.f("fk_anomaly_candidates_scene_id_scenes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_anomaly_candidates_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["zones.id"],
            name=op.f("fk_anomaly_candidates_zone_id_zones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_anomaly_candidates")),
        sa.UniqueConstraint("zone_id", "scene_id", name=op.f("uq_anomaly_candidates_zone_scene")),
    )
    op.create_index(
        op.f("ix_anomaly_candidates_observed_at"), "anomaly_candidates", ["observed_at"]
    )
    op.create_index(op.f("ix_anomaly_candidates_scene_id"), "anomaly_candidates", ["scene_id"])
    op.create_index(
        op.f("ix_anomaly_candidates_water_body_id"), "anomaly_candidates", ["water_body_id"]
    )
    op.create_index(op.f("ix_anomaly_candidates_zone_id"), "anomaly_candidates", ["zone_id"])
    op.create_index(
        "idx_anomaly_candidates_spatial_geom",
        "anomaly_candidates",
        ["spatial_geom"],
        postgresql_using="gist",
    )
    # The alert feed (S8) reads "alertable candidates, newest first" per body.
    op.create_index(
        "ix_anomaly_candidates_alertable_time",
        "anomaly_candidates",
        ["water_body_id", sa.text("observed_at DESC")],
        postgresql_where=sa.text("alertable"),
    )

    op.create_table(
        "anomaly_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("sensed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("n_zones", sa.Integer(), nullable=True),
        sa.Column("n_candidates", sa.Integer(), nullable=True),
        sa.Column("n_flagged", sa.Integer(), nullable=True),
        sa.Column("n_alertable", sa.Integer(), nullable=True),
        sa.Column("n_gated", sa.Integer(), nullable=True),
        sa.Column("spatial_ran", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scenes.id"],
            name=op.f("fk_anomaly_runs_scene_id_scenes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_anomaly_runs_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_anomaly_runs")),
        sa.UniqueConstraint(
            "water_body_id", "scene_id", name=op.f("uq_anomaly_runs_water_body_scene")
        ),
    )
    op.create_index(op.f("ix_anomaly_runs_scene_id"), "anomaly_runs", ["scene_id"])
    op.create_index(op.f("ix_anomaly_runs_sensed_at"), "anomaly_runs", ["sensed_at"])
    op.create_index(op.f("ix_anomaly_runs_water_body_id"), "anomaly_runs", ["water_body_id"])


def downgrade() -> None:
    op.drop_table("anomaly_runs")
    op.drop_table("anomaly_candidates")
