"""add federated bulletin fields

Revision ID: a1b2c3d4e5f6
Revises: e0518ce53aff
Create Date: 2026-07-02

cabal#45 — `kind` + `federation_audience` on the agents bulletin board.
Additive: existing rows backfill to kind='member' via the server_default, so
current dispatch behaviour is unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "e0518ce53aff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default=sa.text("'member'"),
            )
        )
        batch_op.add_column(sa.Column("federation_audience", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_column("federation_audience")
        batch_op.drop_column("kind")
