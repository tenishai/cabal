"""JWKS provider for the dispatch router.

Holds an in-memory KeySet built from the active + retired signing-key rows
and refreshed when `cbl rotate signing-key` runs (broadcast bus is Phase 2;
for Phase 1 we refresh on every lifespan startup and expose a `refresh()`
hook for tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from joserfc.jwk import KeySet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.auth.keys import list_public_jwks
from cabal.core.db import open_session


@dataclass
class RefreshingJWKSProvider:
    """Callable that returns the current KeySet.

    Loaded lazily on first call, refreshable on demand. Thread-safe enough
    for asyncio — we never tear down the KeySet while serving a request.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    _cached: KeySet | None = field(default=None, init=False)

    async def refresh(self) -> KeySet:
        async with open_session(self.sessionmaker) as session:
            jwks = await list_public_jwks(session)
        if not jwks:
            self._cached = KeySet([])
            return self._cached
        self._cached = KeySet.import_key_set({"keys": jwks})
        return self._cached

    def __call__(self) -> KeySet:
        if self._cached is None:
            raise RuntimeError(
                "JWKS provider hasn't been refreshed yet — call refresh() in lifespan"
            )
        return self._cached
