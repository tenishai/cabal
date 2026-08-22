"""Flat caller -> target dispatch-allow pairs.

kobo (the room) owns the policy of which agent may dispatch to which
target. Cabal stores the pairs dumbly and enforces them at the re-mint
step in `routes/tasks.py::_resolve_dispatch_token`.

EMPTY TABLE MEANS OPEN (CABAL-DECISIONS.md D-017): before the room has
ever synced a set of pairs, every agent-to-agent dispatch is allowed.
Enforcement begins the instant the first sync lands via
`PUT /admin/dispatch-pairs` -- deploy-safe, no separate "turn on
enforcement" flag needed.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cabal.core.models import DispatchPair

# Sanity cap on PUT /admin/dispatch-pairs -- guards against an unbounded
# or malformed sync body.
MAX_PAIRS = 100_000


async def is_allowed(session: AsyncSession, caller: str, target: str) -> bool:
    """True if `caller` may dispatch to `target`.

    An empty table means open: every caller may reach every target until
    the room syncs its first set (even a small one).
    """
    count = (await session.execute(select(func.count()).select_from(DispatchPair))).scalar_one()
    if count == 0:
        return True
    result = await session.execute(
        select(DispatchPair.caller).where(
            DispatchPair.caller == caller, DispatchPair.target == target
        )
    )
    return result.scalar_one_or_none() is not None


async def list_all(session: AsyncSession) -> list[tuple[str, str]]:
    """All pairs, in no particular order."""
    rows = (await session.execute(select(DispatchPair.caller, DispatchPair.target))).all()
    return [(row.caller, row.target) for row in rows]


async def replace_all(session: AsyncSession, pairs: list[tuple[str, str]]) -> None:
    """Delete every existing pair and insert `pairs`, atomically.

    Atomicity comes from the caller's transaction: this function only
    stages the delete + inserts on `session` (flush, no commit). As long
    as the caller commits once after this returns -- which is exactly
    what `open_session()` does when wrapping a single route handler --
    another connection can never observe the half-written state (old
    rows gone, new rows not yet visible). That's the property the
    enforcement gate depends on: a momentarily-empty table would read as
    OPEN, so a non-atomic replace would be a real, if brief, security
    hole.
    """
    await session.execute(delete(DispatchPair))
    session.add_all([DispatchPair(caller=caller, target=target) for caller, target in pairs])
    await session.flush()
