"""per-scene BOA add-offset

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-22

Earth Search COGs already have ESA's +1000 BOA offset removed
(``earthsearch:boa_offset_applied``), so subtracting it again clipped every
dark-water pixel to zero. The offset is now decided per scene at ingestion.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scenes",
        sa.Column("boa_add_offset", sa.Integer(), nullable=False, server_default="0"),
    )
    # Existing rows: only CDSE serves raw ESA data that still carries the offset.
    op.execute("UPDATE scenes SET boa_add_offset = -1000 WHERE source = 'cdse'")


def downgrade() -> None:
    op.drop_column("scenes", "boa_add_offset")
