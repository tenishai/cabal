"""add delegation_budgets so a restart cannot re-arm the budget

Revision ID: c2f4a8b61d07
Revises: b7c1d9e04a22
Create Date: 2026-09-12

cabal#174 — `DelegationBudgetGuardrail` held its counter in process memory,
so the guarantee was 40 delegations per conversation PER PROCESS LIFETIME.
That is the path cabal#146's incident actually took: the agent was
liveness-killed, restarted, resumed from persisted state (task state
persists; the counter did not) and rebuilt the storm against a fresh budget.

One row per CONVERSATION key. The caller-keyed class is deliberately absent:
the guard's clock is `time.monotonic`, which resets on restart, so a restored
caller count would sit against a window whose origin is gone. A conversation
key has no TTL and no window, so a count is its whole state.

Additive. An install that never writes a row behaves exactly as before, which
is also what a standalone install with no store does by design.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c2f4a8b61d07"
down_revision: str | None = "b7c1d9e04a22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delegation_budgets",
        sa.Column("session_key", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("session_key"),
    )
    # HYDRATION READS BY RECENCY, not by key: startup restores the most recent
    # rows up to the in-memory LRU ceiling and drops rows past the retention
    # cutoff. Both are ORDER BY / WHERE on updated_at, so it gets an index.
    op.create_index(
        "ix_delegation_budgets_updated_at", "delegation_budgets", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_delegation_budgets_updated_at", table_name="delegation_budgets")
    op.drop_table("delegation_budgets")
