"""add review_runs and review_steps (cabal issue 399)

A review run is one review of one pull-request head, (repo, pr, sha) unique;
its steps are the seats (Temperance, Prudence, ...) in order.

Revision ID: a9d3e5f7c1b2
Revises: f3b6c1d2e4a5
Create Date: 2026-11-13 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d3e5f7c1b2"
down_revision: str | None = "f3b6c1d2e4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_runs",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("pr", sa.Integer(), nullable=False),
        sa.Column("sha", sa.String(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("prev_verdict", sa.JSON(), nullable=True),
        sa.Column("seats", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo", "pr", "sha", name="uq_review_runs_head"),
    )
    op.create_index("ix_review_runs_state", "review_runs", ["state"])
    op.create_table(
        "review_steps",
        sa.Column(
            "run_id",
            sa.String(),
            sa.ForeignKey("review_runs.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("idx", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("seat", sa.String(), nullable=False),
        sa.Column("sha", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("workspace", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("review_steps")
    op.drop_index("ix_review_runs_state", table_name="review_runs")
    op.drop_table("review_runs")
