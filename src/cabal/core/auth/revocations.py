"""Subject-wide token revocation (2026-09-24).

Service tokens are minted without a per-token record (no jti, no store), so
there is nothing to strike one from. What can be said is "every token this
subject was issued before T is dead": one row per subject with a cutoff, kept
in memory for the verification sites (one dict lookup per request, no query)
and in the database so a restart remembers.

The check is `iat < not_before`. A token minted in the same second as the
revocation passes: the room revokes and then mints, and the fresh pass must
not fall to its own revocation.
"""

from __future__ import annotations

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
    `app.state.revocations`; `revoke` writes the row and the cache together."""

    def __init__(self, cutoffs: Mapping[str, int] | None = None) -> None:
        self._not_before: dict[str, int] = dict(cutoffs or {})

    @classmethod
    async def load(cls, sessionmaker: async_sessionmaker[AsyncSession]) -> RevocationList:
        async with open_session(sessionmaker) as session:
            rows = (await session.execute(select(TokenRevocation))).scalars().all()
        return cls({row.sub: int(row.not_before) for row in rows})

    def cutoff(self, sub: str) -> int | None:
        return self._not_before.get(sub)

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
        """Refuse every token `sub` was issued before `not_before` (default:
        now). The cutoff only moves forward: a later call with an earlier
        time changes nothing, so a replay cannot un-revoke."""
        cutoff = int(not_before) if not_before is not None else int(time.time())
        current = self._not_before.get(sub)
        if current is not None and current >= cutoff:
            return current
        row = await session.get(TokenRevocation, sub)
        if row is None:
            session.add(TokenRevocation(sub=sub, not_before=cutoff, updated_at=datetime.now(UTC)))
        else:
            row.not_before = cutoff
            row.updated_at = datetime.now(UTC)
        await session.commit()
        self._not_before[sub] = cutoff
        return cutoff


def refusal(
    request_state_revocations: RevocationList | None, claims: Mapping[str, Any]
) -> str | None:
    """The check every verification site makes, tolerant of an app that has
    no list yet (tests that build routes without the lifespan)."""
    if request_state_revocations is None:
        return None
    return request_state_revocations.refuses(claims)
