"""add token_revocations

Revision ID: d7e1f3a9b2c4
Revises: c4f0a2b18e77
Create Date: 2026-09-24 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e1f3a9b2c4"
down_revision: str | None = "c4f0a2b18e77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "token_revocations",
        sa.Column("sub", sa.String(), primary_key=True, nullable=False),
        sa.Column("not_before", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("token_revocations")
