"""candidate_scores and priority_models

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "priority_models",
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("n_training", sa.Integer(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("feature_names", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("kind IN ('weighted', 'xgboost')", name=op.f("ck_priority_models_kind")),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_priority_models")),
    )
    # At most one active model at a time.
    op.create_index(
        "uq_priority_models_active",
        "priority_models",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "candidate_scores",
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("zone_id", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority_score", sa.REAL(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("severity_capped_from", sa.Text(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("confidence_parts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("primary_indicator", sa.Text(), nullable=True),
        sa.Column("alertable", sa.Boolean(), nullable=False),
        sa.Column("natural_cause_likely", sa.Boolean(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("model_base", sa.Float(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contributions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "priority_score >= 0 AND priority_score <= 100", name=op.f("ck_candidate_scores_score")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_candidate_scores_confidence")
        ),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN ('low', 'medium', 'high')",
            name=op.f("ck_candidate_scores_severity"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["anomaly_candidates.id"],
            name=op.f("fk_candidate_scores_candidate_id_anomaly_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scenes.id"],
            name=op.f("fk_candidate_scores_scene_id_scenes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_candidate_scores_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["zones.id"],
            name=op.f("fk_candidate_scores_zone_id_zones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("candidate_id", name=op.f("pk_candidate_scores")),
    )
    op.create_index(op.f("ix_candidate_scores_model_version"), "candidate_scores", ["model_version"])
    op.create_index(op.f("ix_candidate_scores_observed_at"), "candidate_scores", ["observed_at"])
    op.create_index(op.f("ix_candidate_scores_scene_id"), "candidate_scores", ["scene_id"])
    op.create_index(op.f("ix_candidate_scores_water_body_id"), "candidate_scores", ["water_body_id"])
    op.create_index(op.f("ix_candidate_scores_zone_id"), "candidate_scores", ["zone_id"])
    # The alert list is "alertable, priority descending".
    op.create_index(
        "ix_candidate_scores_priority",
        "candidate_scores",
        [sa.text("priority_score DESC"), sa.text("observed_at DESC")],
        postgresql_where=sa.text("alertable"),
    )


def downgrade() -> None:
    op.drop_table("candidate_scores")
    op.drop_table("priority_models")
