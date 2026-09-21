"""alerts, recipients, dispatches

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _geom(nullable: bool) -> sa.Column:
    return sa.Column(
        "geom" if not nullable else "jurisdiction_geom",
        geoalchemy2.types.Geometry(
            geometry_type="MULTIPOLYGON",
            srid=4326,
            spatial_index=False,
            from_text="ST_GeomFromEWKT",
            name="geometry",
        ),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("zone_id", sa.Text(), nullable=False),
        sa.Column("primary_indicator", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("priority_score", sa.REAL(), nullable=False),
        sa.Column("peak_priority_score", sa.REAL(), nullable=False),
        sa.Column("peak_severity", sa.Text(), nullable=False),
        sa.Column("natural_cause_likely", sa.Boolean(), nullable=False),
        sa.Column("affected_area_km2", sa.Float(), nullable=True),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_candidate_id", sa.Integer(), nullable=True),
        sa.Column("latest_scene_id", sa.Text(), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("contributions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("timeline", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _geom(nullable=False),
        sa.Column("brief_key", sa.Text(), nullable=True),
        sa.Column("brief_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("brief_for_observation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_severity", sa.Text(), nullable=True),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status_changed_by", sa.Text(), nullable=True),
        sa.Column("status_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('open', 'investigating', 'validated', 'dismissed')",
            name=op.f("ck_alerts_status"),
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high')", name=op.f("ck_alerts_severity")
        ),
        sa.ForeignKeyConstraint(
            ["latest_candidate_id"],
            ["anomaly_candidates.id"],
            name=op.f("fk_alerts_latest_candidate_id_anomaly_candidates"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["latest_scene_id"],
            ["scenes.id"],
            name=op.f("fk_alerts_latest_scene_id_scenes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_alerts_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name=op.f("fk_alerts_zone_id_zones"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
    )
    op.create_index(op.f("ix_alerts_last_observed_at"), "alerts", ["last_observed_at"])
    op.create_index(op.f("ix_alerts_priority_score"), "alerts", ["priority_score"])
    op.create_index(op.f("ix_alerts_status"), "alerts", ["status"])
    op.create_index(op.f("ix_alerts_water_body_id"), "alerts", ["water_body_id"])
    op.create_index(op.f("ix_alerts_zone_id"), "alerts", ["zone_id"])
    op.create_index("idx_alerts_geom", "alerts", ["geom"], postgresql_using="gist")
    # Dedup lookup: the open alert for a (zone, indicator).
    op.create_index(
        "ix_alerts_open_zone_indicator",
        "alerts",
        ["zone_id", "primary_indicator", sa.text("last_observed_at DESC")],
        postgresql_where=sa.text("status IN ('open', 'investigating')"),
    )

    op.create_table(
        "recipients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("min_severity", sa.Text(), nullable=False),
        sa.Column("districts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _geom(nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("channel IN ('webhook', 'email')", name=op.f("ck_recipients_channel")),
        sa.CheckConstraint(
            "min_severity IN ('low', 'medium', 'high')", name=op.f("ck_recipients_min_severity")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipients")),
    )
    op.create_index(
        "idx_recipients_jurisdiction_geom",
        "recipients",
        ["jurisdiction_geom"],
        postgresql_using="gist",
    )

    op.create_table(
        "dispatches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.Text(), nullable=False),
        sa.Column("recipient_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"], name=op.f("fk_dispatches_alert_id_alerts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["recipients.id"],
            name=op.f("fk_dispatches_recipient_id_recipients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dispatches")),
    )
    op.create_index(op.f("ix_dispatches_alert_id"), "dispatches", ["alert_id"])
    op.create_index(op.f("ix_dispatches_recipient_id"), "dispatches", ["recipient_id"])


def downgrade() -> None:
    op.drop_table("dispatches")
    op.drop_table("recipients")
    op.drop_table("alerts")
