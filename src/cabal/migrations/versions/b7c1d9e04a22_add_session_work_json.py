"""add work_json for session presence rows

Revision ID: b7c1d9e04a22
Revises: a1b2c3d4e5f6
Create Date: 2026-09-10

cabal#142 — a `kind='session'` bulletin row records what a live Claude Code
session is currently doing (repo, branch, one-line task) so `cbl status` can
answer "why do I have three Poes and what are they doing".

Additive and nullable: every existing row keeps `work_json = NULL`, which
renders as no work line. Nothing about dispatch or personas changes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1d9e04a22"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("work_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_column("work_json")
