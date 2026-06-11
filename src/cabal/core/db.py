"""SQLite + SQLAlchemy 2.0 async engine.

The engine is constructed once per process. Tests substitute an in-memory
SQLite URL; the daemon defaults to a file under `data/`.

Litestream replication runs as a sidecar in production deployments and is
invisible to this module (it operates against the file directly).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


DEFAULT_DB_URL = "sqlite+aiosqlite:///data/cabal.db"


def make_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine for the given SQLite URL.

    Pragmas (foreign_keys, journal_mode=WAL, synchronous=NORMAL) are applied
    on every connect via `connect_args` + an event listener registered in
    `cabal.core.db_pragmas`.
    """
    url = url or os.environ.get("CABAL_DB_URL") or DEFAULT_DB_URL
    return create_async_engine(
        url,
        echo=echo,
        # SQLite + asyncio requires future=True (default in SA 2.0) and
        # connect_args to opt out of same-thread checks.
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def open_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
