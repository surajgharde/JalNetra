"""validation snapshot columns and baseline_samples

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("validations", sa.Column("alert_severity", sa.Text(), nullable=True))
    op.add_column("validations", sa.Column("alert_indicator", sa.Text(), nullable=True))
    op.add_column("validations", sa.Column("alert_priority_score", sa.REAL(), nullable=True))
    op.add_column("validations", sa.Column("alert_observed_on", sa.Date(), nullable=True))
    op.add_column("validations", sa.Column("candidate_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_validations_candidate_id_anomaly_candidates"),
        "validations",
        "anomaly_candidates",
        ["candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_validations_verdict", "validations", ["verdict"])

    op.create_table(
        "baseline_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("zone_id", sa.Text(), nullable=False),
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.REAL(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("validation_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["validation_id"],
            ["validations.id"],
            name=op.f("fk_baseline_samples_validation_id_validations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name=op.f("fk_baseline_samples_zone_id_zones"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_baseline_samples")),
    )
    op.create_index(op.f("ix_baseline_samples_zone_id"), "baseline_samples", ["zone_id"])
    op.create_index(
        "ix_baseline_samples_series", "baseline_samples", ["zone_id", "indicator", "observed_at"]
    )


def downgrade() -> None:
    op.drop_table("baseline_samples")
    op.drop_index("ix_validations_verdict", table_name="validations")
    op.drop_constraint(
        op.f("fk_validations_candidate_id_anomaly_candidates"), "validations", type_="foreignkey"
    )
    for col in ("candidate_id", "alert_observed_on", "alert_priority_score", "alert_indicator", "alert_severity"):
        op.drop_column("validations", col)
