"""DelegationBudgetStore Protocol + default SQLite implementation.

cabal#174. `DelegationBudgetGuardrail` bounds how many delegations one
conversation may make, and held that count in process memory only -- so the
bound was per conversation PER PROCESS LIFETIME. cabal#146's incident ended
with the agent liveness-killed, restarted, and resumed from persisted state:
the task came back, the counter did not, and the storm was rebuilt against a
fresh budget.

Same Protocol-over-impl shape as `CheckpointStore` and `SecretsBackend`, and
for the same reason: a Postgres or Redis backend plugs in without the guard
changing, and `NoOpDelegationBudgetStore` keeps a standalone install (and the
test suite) from needing a database to get a bounded guard.

WHY THE HOT PATH NEVER CALLS THIS DIRECTLY
------------------------------------------

`DelegationBudgetGuardrail.check()` must remain await-free. A primus fan-out
calls it concurrently across legs sharing one guard instance, and an
await-free `async def` runs atomically to completion on a cooperative event
loop -- that atomicity is the only thing protecting the counter from a data
race. `cabal.core.db` is `sqlite+aiosqlite`, so every method here is an
`await` and none of them may be awaited from `check()`.

So the guard uses this store at two points that are NOT the hot path:

  - `hydrate()` once at startup, before the app serves anything.
  - a write-behind scheduled from `check()` via `asyncio.create_task`, which
    is not a yield point, so the atomicity invariant survives.

A write-behind can lose its last few increments to a SIGKILL. That is the
accepted trade and worth stating plainly: this bounds a storm, it does not do
accounting. Losing three increments off a budget of forty changes nothing;
losing all forty is the bug being fixed.

CONVERSATION KEYS ONLY
----------------------

The guard's `_now` is `time.monotonic`, which resets on restart. A
caller-keyed entry carries a `window_start` on that clock, so restoring one
would mean restoring a count against a window whose origin no longer exists.
A conversation key has no TTL and no window by design, so its count is its
whole state. That is also the class a resumed task falls into, which is why
the narrow scope is the right one rather than a compromise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import open_session

__all__ = [
    "DelegationBudgetStore",
    "NoOpDelegationBudgetStore",
    "SqliteDelegationBudgetStore",
]


@runtime_checkable
class DelegationBudgetStore(Protocol):
    """Durable home for conversation-keyed delegation counts."""

    async def bump(self, session_key: str, count: int) -> None:
        """Record `count` for `session_key`, never lowering a stored value.

        MONOTONIC ON PURPOSE. The in-memory counter is not: it carries an LRU
        whose eviction is a known, documented refill path. If a write-behind
        could lower the stored count, an eviction would wipe the durable
        record too and re-open exactly the hole this store closes.
        """
        ...

    async def load_recent(
        self, *, limit: int, retention_s: float
    ) -> list[tuple[str, int]]:
        """Drop rows past the retention cutoff, then return the most recent
        `limit` surviving rows as `(session_key, count)`, OLDEST FIRST.

        Oldest-first matters: the caller replays these into an insertion-
        ordered LRU, so the last one inserted must be the most recently used.
        """
        ...


class NoOpDelegationBudgetStore:
    """Default store -- keeps the guard's behaviour exactly as it was before
    cabal#174 (bounded in memory, re-armed by a restart) without making every
    call site check for `None`."""

    async def bump(self, session_key: str, count: int) -> None:  # noqa: ARG002
        return None

    async def load_recent(
        self, *, limit: int, retention_s: float
    ) -> list[tuple[str, int]]:  # noqa: ARG002
        return []


class SqliteDelegationBudgetStore:
    """Default store -- the same SQLite database as the rest of cabal core."""

    __slots__ = ("sessionmaker",)

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def bump(self, session_key: str, count: int) -> None:
        # Imported here rather than at module load, matching SqliteCheckpointStore:
        # keeps the ORM import off this module's import graph.
        from cabal.core.models import DelegationBudget

        async with open_session(self.sessionmaker) as session:
            stmt = sqlite_insert(DelegationBudget).values(
                session_key=session_key,
                count=count,
                updated_at=datetime.now(UTC),
            )
            # `max(stored, incoming)` -- SQLite's two-argument max is the scalar
            # one, not the aggregate. This is where the monotonic property in
            # the Protocol docstring is actually enforced.
            stmt = stmt.on_conflict_do_update(
                index_elements=[DelegationBudget.session_key],
                set_={
                    "count": func.max(DelegationBudget.count, stmt.excluded.count),
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def load_recent(
        self, *, limit: int, retention_s: float
    ) -> list[tuple[str, int]]:
        from cabal.core.models import DelegationBudget

        cutoff = datetime.now(UTC) - timedelta(seconds=retention_s)
        async with open_session(self.sessionmaker) as session:
            # PRUNE FIRST so the recency window is not spent on rows that are
            # about to be dropped. A conversation key has no TTL in memory only
            # because the LRU is its ceiling; a table has no such ceiling, so
            # retention is what stands in for one.
            await session.execute(
                delete(DelegationBudget).where(DelegationBudget.updated_at < cutoff)
            )
            await session.commit()
            result = await session.execute(
                select(DelegationBudget.session_key, DelegationBudget.count)
                .order_by(DelegationBudget.updated_at.desc())
                .limit(limit)
            )
            rows = list(result.all())
        # Fetched newest-first so `limit` keeps the newest; returned oldest-first
        # so the replay leaves the newest most-recently-used in the LRU.
        return [(key, count) for key, count in reversed(rows)]
