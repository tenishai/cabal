"""Flat caller -> target dispatch-allow pairs.

kobo (the room) owns the policy of which agent may dispatch to which
target. Cabal stores the pairs dumbly and enforces them in the one re-mint
policy every network door calls, `routes/_dispatch_auth.dispatch_token`.
A policy enforced on some doors is not enforced.

EMPTY TABLE MEANS OPEN (CABAL-DECISIONS.md D-018): before the room has
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


def dedupe_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order-preserving de-duplication of (caller, target) pairs.

    A sync body listing the same pair twice is not a client error -- it's
    the same statement made twice. Keep the first occurrence and drop the
    rest so `replace_all` never asks the database to insert the same
    composite primary key twice.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        deduped.append(pair)
    return deduped


async def replace_all(
    session: AsyncSession, pairs: list[tuple[str, str]]
) -> list[tuple[str, str]]:
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

    `pairs` is de-duplicated first (order-preserving, see `dedupe_pairs`)
    -- a body with the same (caller, target) pair twice is not an error,
    and inserting it twice would hit the composite primary key. Returns
    the de-duplicated list that was actually staged, so a caller can
    report an accurate count without keeping its own copy in sync.
    """
    deduped = dedupe_pairs(pairs)
    await session.execute(delete(DispatchPair))
    session.add_all([DispatchPair(caller=caller, target=target) for caller, target in deduped])
    await session.flush()
    return deduped
