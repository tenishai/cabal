"""add meter_spend (cabal issue 394, part 2)

One row per task on a metered agent, with the tokens the meter measured, plus
one `unattributed` and one `ambiguous` row per agent.

Revision ID: c6e1f4a8b2d9
Revises: b4c8e2a6d1f3
Create Date: 2026-11-16 14:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6e1f4a8b2d9"
down_revision: str | None = "b4c8e2a6d1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meter_spend",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("root_id", sa.String(), nullable=True),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("refused_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("reported", sa.JSON(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent", "kind", "task_id", name="uq_meter_spend_task"),
    )
    op.create_index("ix_meter_spend_root", "meter_spend", ["root_id"])


def downgrade() -> None:
    op.drop_index("ix_meter_spend_root", table_name="meter_spend")
    op.drop_table("meter_spend")
