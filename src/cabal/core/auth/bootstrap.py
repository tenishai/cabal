"""Startup-time auth bootstrap.

On daemon startup: if no active signing key exists, generate one. This is
the "first-touch" path — fresh deployment with empty DB. Subsequent starts
are no-ops (an active key is already in the DB).
"""

from __future__ import annotations

import structlog

from cabal.core.auth.keys import create_signing_key, load_active_signing_key
from cabal.core.db import open_session
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

log = structlog.get_logger(__name__)


async def ensure_signing_key(
    sessionmaker: async_sessionmaker[AsyncSession], *, master_key: bytes
) -> str:
    """Return the kid of the active signing key, creating one if needed."""
    async with open_session(sessionmaker) as session:
        existing = await load_active_signing_key(session, master_key=master_key)
        if existing is not None:
            log.debug("auth.signing_key.existing", kid=existing.kid)
            return existing.kid
        created = await create_signing_key(session, master_key=master_key)
        log.info("auth.signing_key.created", kid=created.kid)
        return created.kid
