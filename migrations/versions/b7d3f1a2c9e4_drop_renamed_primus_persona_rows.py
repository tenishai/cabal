"""drop orphaned persona config rows left by primus renames

Revision ID: b7d3f1a2c9e4
Revises: a1b2c3d4e5f6
Create Date: 2026-08-01

`personas` is a CONFIG projection, re-seeded from identity.toml at boot by
`identity_sync.sync_personas` — which has no delete path. So renaming a persona
leaves its old row behind forever. Proof already in the wild: `corax` (a
previous rename's orphan, created 2026-07-04) still sits in the table with zero
references anywhere in the codebase.

This drops the rows made stale by `rook` -> `mason` / `jackdaw` -> `wright`,
plus that pre-existing `corax` orphan. `sync_personas` recreates `mason` and
`wright` from identity.toml on the next boot, so the live set is unchanged.

DELIBERATELY NOT TOUCHED — `conversations` and `events`.
Those reference agent names too (locally: 4 conversations against
`rook-primus`, 5 events whose payload names a renamed persona), but they are an
AUDIT RECORD of what actually ran. A task really was dispatched to
`rook-primus`; rewriting those rows to say `mason-primus` would falsify the
execution history to make it tidy. Stale names in an audit log are correct.
The same reasoning keeps docs/proposals unrewritten in this PR.

Consequence to expect: a name in `conversations`/`events` may no longer resolve
to a row in `personas`. There is no FK between them, so nothing dangles at the
schema level — the join is best-effort by design, and history predating a
rename is exactly when it should miss.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = 'b7d3f1a2c9e4'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Config rows only. Named explicitly rather than pattern-matched so this cannot
# delete a persona an operator legitimately added.
_STALE = ('rook', 'jackdaw', 'corax')


def upgrade() -> None:
    op.execute(
        "DELETE FROM personas WHERE name IN (%s)"
        % ", ".join(f"'{n}'" for n in _STALE)
    )


def downgrade() -> None:
    # Irreversible by design: the deleted rows were derived config, and their
    # config_json is not recoverable from here. Re-running the daemon against an
    # identity.toml that declares those personas is the way back — which is the
    # same mechanism that created them. A no-op downgrade is honest; fabricating
    # empty rows would be worse than leaving them absent.
    pass
