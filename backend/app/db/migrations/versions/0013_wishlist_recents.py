"""wishlist_items and recent_water_bodies

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-26

UI state over the registry: both tables only reference water_bodies, so nothing
the pipeline reads or writes changes. Dropping a water body cascades to both.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wishlist_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column("custom_name", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_wishlist_items_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wishlist_items")),
        # Saving the same lake twice is a rename, not a second row.
        sa.UniqueConstraint("water_body_id", name="uq_wishlist_items_water_body_id"),
    )
    op.create_index(
        op.f("ix_wishlist_items_water_body_id"), "wishlist_items", ["water_body_id"], unique=False
    )

    op.create_table(
        "recent_water_bodies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("water_body_id", sa.Text(), nullable=False),
        sa.Column(
            "last_viewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["water_body_id"],
            ["water_bodies.id"],
            name=op.f("fk_recent_water_bodies_water_body_id_water_bodies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recent_water_bodies")),
        sa.UniqueConstraint("water_body_id", name=op.f("uq_recent_water_bodies_water_body_id")),
    )
    # The list is always "most recent N", so the ordering column carries the index.
    op.create_index(
        op.f("ix_recent_water_bodies_last_viewed_at"),
        "recent_water_bodies",
        ["last_viewed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_recent_water_bodies_last_viewed_at"), table_name="recent_water_bodies")
    op.drop_table("recent_water_bodies")
    op.drop_index(op.f("ix_wishlist_items_water_body_id"), table_name="wishlist_items")
    op.drop_table("wishlist_items")
