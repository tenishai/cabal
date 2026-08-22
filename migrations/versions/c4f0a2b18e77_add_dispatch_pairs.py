"""add dispatch_pairs

Revision ID: c4f0a2b18e77
Revises: a1b2c3d4e5f6
Create Date: 2026-08-22 07:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f0a2b18e77"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dispatch_pairs",
        sa.Column("caller", sa.String(), primary_key=True, nullable=False),
        sa.Column("target", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("dispatch_pairs")
