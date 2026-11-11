"""add conversations.not_before and schedule_json (cabal issue 406)

A dispatch can ask to start later: its row waits in state `scheduled` with
`not_before` set and the caller's claims in `schedule_json` (no token).

Revision ID: f3b6c1d2e4a5
Revises: d7e1f3a9b2c4
Create Date: 2026-11-10 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b6c1d2e4a5"
down_revision: str | None = "d7e1f3a9b2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("schedule_json", sa.JSON(), nullable=True))
        batch_op.create_index("idx_conversations_schedule", ["state", "not_before"])


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index("idx_conversations_schedule")
        batch_op.drop_column("schedule_json")
        batch_op.drop_column("not_before")
