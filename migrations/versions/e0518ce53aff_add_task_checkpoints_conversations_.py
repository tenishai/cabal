"""add task_checkpoints + conversations.checkpoint_id

Revision ID: e0518ce53aff
Revises: 1a19a7317962
Create Date: 2026-06-12 22:11:01.133995
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e0518ce53aff"
down_revision: str | None = "1a19a7317962"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_checkpoints",
        sa.Column("task_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("messages_json", sa.JSON(), nullable=False),
        sa.Column("task_json", sa.JSON(), nullable=True),
        sa.Column("persona_state_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("checkpoint_id", sa.String(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_conversations_checkpoint_id",
            "task_checkpoints",
            ["checkpoint_id"],
            ["task_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_conversations_checkpoint_id", type_="foreignkey"
        )
        batch_op.drop_column("checkpoint_id")
    op.drop_table("task_checkpoints")
