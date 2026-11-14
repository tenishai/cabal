"""add conversations.target_group (cabal issue 423)

A task dispatched to a group waits in state `scheduled` with `target_group`
set. The Scheduler starts it on the first idle member, and moves
`target_agent` to that member when it claims the row.

Revision ID: b4c8e2a6d1f3
Revises: a9d3e5f7c1b2
Create Date: 2026-11-14 01:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c8e2a6d1f3"
down_revision: str | None = "a9d3e5f7c1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target_group", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("target_group")
