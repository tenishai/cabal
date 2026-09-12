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

The guard reads `time.monotonic()` directly, in `check()` and in `hydrate()`,
and that clock resets on restart. (It has no injectable `_now` seam -- an
earlier version of this paragraph said it did, naming one that belongs to
`DoomLoopGuardrail`. The substance was right and the citation pointed at the
wrong class; #175 r1 nit 2.) A caller-keyed entry carries a `window_start` on
that clock, so restoring one would mean restoring a count against a window
whose origin no longer exists. A conversation key has no TTL and no window by
design, so its count is its whole state.

SCOPED, NOT COMPLETE, AND THE DIFFERENCE MATTERS (#175 r1 Important). A
**threaded** resumed task falls into the conversation class and is covered. An
UN-THREADED one is not: `Task.context_id` defaults to `None`, `check()` then
falls back to the caller key with `windowed=True`, and the persist is skipped
-- so **every caller-keyed budget is still zeroed by every restart**, with no
checkpoint or resume involved. `cbl dispatch` is that live path; `cbl chat`
threads a contextId.

That is a deliberate accounting, not an oversight: the cost is one extra
window of a rolling rate limit, which is the failure mode a windowed budget
already has by design, and persisting a monotonic `window_start` would be
incoherent. But it is a narrower result than "the scope is complete," which
is what this docstring used to claim. An operator reading the old wording
would conclude cabal#174 was closed for everything that can resume. It is
closed for threaded conversations.
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
        self, *, limit: int, retention_s: float, keep_at_or_above: int | None = None
    ) -> list[tuple[str, int]]:
        """Prune, then return the most recent `limit` surviving rows as
        `(session_key, count)`, OLDEST FIRST.

        Oldest-first matters: the caller replays these into an insertion-
        ordered LRU, so the last one inserted must be the most recently used.

        `keep_at_or_above` EXEMPTS AN EXHAUSTED COUNT FROM TIME-BASED PRUNING,
        and the concordance audit on cabal#175 is why it exists. Thirteen
        carriers jointly assert that a conversation key has no time-based
        reset and that the in-memory LRU is the ONE exception -- an enumeration
        the class docstring states as complete. A retention cutoff on
        `updated_at` (refreshed by every `bump`, so idle-keyed) is a SECOND
        time-based erasure: a conversation idle past the cutoff that then
        survives a restart would be handed a fresh count, which is a refill
        caused by elapsed time on a key documented as having none.

        Rather than caveat every carrier, the mechanism is narrowed so the
        claim stays true where it matters: a count that reached the limit is
        never pruned by time, so an exhausted conversation stays exhausted
        across a restart however long it sat. Partial counts still lapse, and
        the growth the exemption allows is bounded by the recency cap below,
        which is the DURABLE ANALOGUE of the LRU retirement the design already
        documents and accepts, not a third mechanism.

        PRUNING IS A STARTUP-TIME PROPERTY, NOT A STEADY-STATE ONE (#175 r1
        nit 6). `hydrate()` is this method's only production caller, so while a
        process is up the table grows one row per conversation and collapses to
        the cap only at the next restart. Measured in review, the startup prune
        stays cheap well past any realistic row count; the number is in that
        review rather than here, because a measurement in a comment has nothing
        to invalidate it (REVIEW-PROCESS.md §4 rule 2).

        `keep_at_or_above` IS NOT SYMMETRIC UNDER A CHANGED LIMIT (#175 r1,
        measured by the reviewer). Lowering the limit keeps a row that would
        otherwise have lapsed, which is tighter. RAISING it reclassifies a
        stored count from exhausted to partial -- a stored 11 under a new limit
        of 40 -- and that row then lapses on idle, which is looser. It takes an
        operator deliberately raising a limit to reach, and the looser outcome
        is the "partial counts lapse" behaviour already documented above; it is
        recorded because this docstring makes a claim about the exemption and
        the claim needs the caveat.
        """
        ...


class NoOpDelegationBudgetStore:
    """Default store -- keeps the guard's behaviour exactly as it was before
    cabal#174 (bounded in memory, re-armed by a restart) without making every
    call site check for `None`."""

    async def bump(self, session_key: str, count: int) -> None:  # noqa: ARG002
        return None

    async def load_recent(
        self, *, limit: int, retention_s: float, keep_at_or_above: int | None = None
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
        self, *, limit: int, retention_s: float, keep_at_or_above: int | None = None
    ) -> list[tuple[str, int]]:
        from cabal.core.models import DelegationBudget

        cutoff = datetime.now(UTC) - timedelta(seconds=retention_s)
        async with open_session(self.sessionmaker) as session:
            # PRUNE FIRST so the recency window is not spent on rows that are
            # about to be dropped.
            #
            # AN EXHAUSTED COUNT IS EXEMPT FROM THE TIME CUTOFF (cabal#175
            # concordance). `updated_at` is refreshed by every `bump`, so this
            # predicate is idle-keyed -- without the exemption a conversation
            # idle past the cutoff would come back from a restart with a fresh
            # count, a time-caused refill on a key thirteen carriers say has
            # none. See the Protocol docstring for why narrowing the mechanism
            # beat caveating all thirteen.
            time_prune = delete(DelegationBudget).where(
                DelegationBudget.updated_at < cutoff
            )
            if keep_at_or_above is not None:
                time_prune = time_prune.where(
                    DelegationBudget.count < keep_at_or_above
                )
            await session.execute(time_prune)
            # AND THEN A RECENCY CAP, which is what keeps the exemption from
            # growing the table without bound. Deliberately the same shape as
            # the in-memory LRU at `_BUDGET_MAXSIZE`: a cold key retires. That
            # path is already documented and accepted as the one exception, so
            # this adds no mechanism a reader has to be told about separately.
            keep = select(DelegationBudget.session_key).order_by(
                DelegationBudget.updated_at.desc()
            ).limit(limit)
            await session.execute(
                delete(DelegationBudget).where(
                    DelegationBudget.session_key.not_in(keep)
                )
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
