"""Apply SQLite pragmas on every connection.

SQLite enforces foreign keys only when `PRAGMA foreign_keys=ON` is set per
connection. WAL + NORMAL is the Litestream-friendly default.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine


def install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Register a connect listener that sets pragmas on each new connection."""

    sync_engine: Engine = engine.sync_engine

    @event.listens_for(sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
