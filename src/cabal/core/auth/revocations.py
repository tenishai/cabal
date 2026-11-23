"""Subject-wide token revocation (2026-09-24).

Service tokens are minted without a per-token record (no jti, no store), so
there is nothing to strike one from. What can be said is "every token this
subject was issued before T is dead": one row per subject with a cutoff, kept
in memory for the verification sites (one dict lookup per request, no query)
and in the database so a restart remembers.

The check is `iat < not_before`, and `iat` is whole seconds, so two tokens
issued in the same second cannot be told apart. Two rules keep the old and
the new pass on opposite sides of the cutoff (PR #221 review):

- A revocation with no explicit time takes effect at the START OF THE NEXT
  SECOND (`not_before = now + 1`), so every token issued up to and including
  the current second is refused, even one minted a moment before the call.
- `mint-service` waits out that second (`wait_until_clear`) before it mints
  for a revoked subject, so a pass minted after the revoke has
  `iat >= not_before` and passes. The room revokes and then mints; the wait
  costs it at most one second, and only right after a revoke.

The list lives in this process's memory (loaded from the table at startup,
written through on revoke). That is correct for one uvicorn worker, which is
how cabal-core runs; a second worker would not see the first one's revokes
until it restarts, so adding workers means moving this lookup to the table.

Tokens that the gateway verifies itself (`aud=liminal`, fifteen minutes)
are not checked here; one exchanged before a revoke lives out its TTL. The
exchange route checks this list, so a revoked pass cannot get a new one.
The gateway can now fetch every cutoff from `GET /auth/revocations` and
refuse such tokens itself.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import open_session
from cabal.core.models import TokenRevocation


class RevocationList:
    """The cutoffs, by subject. Shared by every verification site through
    `app.state.revocations`; `revoke` writes the row and the cache together.
    In-memory per process: correct for cabal-core's single worker, not for
    several (see the module docstring)."""

    def __init__(self, cutoffs: Mapping[str, int] | None = None) -> None:
        self._not_before: dict[str, int] = dict(cutoffs or {})
        # Serializes revoke(): without it two first-time revokes of one sub
        # could both see no row, both INSERT, and the second hit the primary
        # key. One process owns the list, so a process lock is enough.
        self._lock = asyncio.Lock()

    @classmethod
    async def load(cls, sessionmaker: async_sessionmaker[AsyncSession]) -> RevocationList:
        async with open_session(sessionmaker) as session:
            rows = (await session.execute(select(TokenRevocation))).scalars().all()
        return cls({row.sub: int(row.not_before) for row in rows})

    def cutoff(self, sub: str) -> int | None:
        return self._not_before.get(sub)

    def snapshot(self) -> dict[str, int]:
        """Every cutoff, by subject, as a copy the caller may keep or change
        (`GET /auth/revocations` serves it to the gateway)."""
        return dict(self._not_before)

    def refuses(self, claims: Mapping[str, Any]) -> str | None:
        """The reason a verified token is refused anyway, or None.

        A token with no readable `sub` or `iat` is not this list's to judge
        (verify() already requires `iat`); it passes here and fails there."""
        sub = claims.get("sub")
        iat = claims.get("iat")
        if not isinstance(sub, str) or iat is None:
            return None
        cutoff = self._not_before.get(sub)
        if cutoff is None:
            return None
        try:
            issued = int(iat)
        except (TypeError, ValueError):
            return None
        if issued < cutoff:
            return f"token for {sub!r} was revoked: issued before its revocation"
        return None

    async def revoke(self, session: AsyncSession, sub: str, not_before: int | None = None) -> int:
        """Refuse every token `sub` was issued before `not_before`. The
        default is the start of the next second, so everything issued up to
        and including this second is refused (see the module docstring). The
        cutoff only moves forward: a later call with an earlier time changes
        nothing, so a replay cannot un-revoke."""
        cutoff = int(not_before) if not_before is not None else int(time.time()) + 1
        async with self._lock:
            current = self._not_before.get(sub)
            if current is not None and current >= cutoff:
                return current
            row = await session.get(TokenRevocation, sub)
            if row is None:
                session.add(
                    TokenRevocation(sub=sub, not_before=cutoff, updated_at=datetime.now(UTC))
                )
            else:
                row.not_before = cutoff
                row.updated_at = datetime.now(UTC)
            await session.commit()
            self._not_before[sub] = cutoff
            return cutoff

    async def wait_until_clear(self, sub: str, *, max_wait: float = 2.0) -> bool:
        """Wait until a token minted now for `sub` would pass this list.

        True once `time.time() >= cutoff(sub)` (at once when `sub` has no
        cutoff or it is already past). False, without waiting, when the
        cutoff is more than `max_wait` seconds away: the operator revoked
        `sub` into the future, and a token minted before then is dead on
        arrival."""
        cutoff = self._not_before.get(sub)
        if cutoff is None:
            return True
        delay = cutoff - time.time()
        if delay > max_wait:
            return False
        while delay > 0:
            await _sleep(delay)
            delay = cutoff - time.time()
        return True


async def _sleep(seconds: float) -> None:
    # A seam so tests can drive a frozen clock through the wait.
    await asyncio.sleep(seconds)


def refusal(
    request_state_revocations: RevocationList | None, claims: Mapping[str, Any]
) -> str | None:
    """The check every verification site makes, tolerant of an app that has
    no list yet (tests that build routes without the lifespan)."""
    if request_state_revocations is None:
        return None
    return request_state_revocations.refuses(claims)
