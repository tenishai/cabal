"""Offline recovery from a master key that no longer matches the database.

THE CONSTRAINT THAT SHAPES THIS MODULE: it must work when cabal CANNOT BOOT.
A mismatched key fails in `lifespan` before the HTTP server binds, so recovery
cannot go through the API the way every other mutation does (cabal#92). It
talks to the SQLite file directly, with stdlib sqlite3, and takes a backup
first because the alternative on the day it goes wrong is nothing.

Deliberately NOT symmetric about what it clears:

    signing_keys  — regenerated on next boot, costs one `cbl login`. Cleared.
    secrets       — every stored handler credential, unrecoverable once the
                    key that encrypted them is gone. NOT cleared by default;
                    reported, and removed only under an explicit flag.

That asymmetry is the point. Un-bricking the daemon and destroying the
operator's credentials are different acts and should not share a default.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

#: Tables this module knows how to clear, in the order it clears them.
_SIGNING_KEYS = "signing_keys"
_SECRETS = "secrets"


class ResetError(RuntimeError):
    """Raised when the reset cannot be performed safely."""


@dataclass(frozen=True)
class ResetReport:
    """What the reset actually did — every field is a fact, not an intent."""

    db_path: Path
    backup_path: Path
    signing_keys_deleted: int
    secrets_purged: int
    secrets_remaining: int
    token_removed: bool


def sqlite_path_from_url(db_url: str) -> Path:
    """Filesystem path behind a SQLAlchemy SQLite URL.

    Raises for any non-SQLite URL rather than guessing: this module's whole
    method (file copy, stdlib sqlite3) is SQLite-specific, and silently
    mis-parsing a Postgres URL would delete nothing while reporting success.
    """
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if db_url.startswith(prefix):
            return Path(db_url[len(prefix) :])
    raise ResetError(
        f"reset-auth only supports SQLite databases; got {db_url!r}. "
        "Clear the signing_keys table by hand on other backends."
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def backup_database(db_path: Path, *, now: datetime | None = None) -> Path:
    """Consistent copy of *db_path*, WAL included.

    Uses sqlite3's own backup API rather than `cp`: the live database keeps its
    recent writes in `-wal`, so copying only the `.db` file can produce a
    backup that is missing exactly the rows the operator is about to delete.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup_path


def reset_auth(
    db_url: str,
    *,
    purge_secrets: bool = False,
    token_path: Path | None = None,
) -> ResetReport:
    """Clear the signing keys so the next boot mints a fresh one.

    The cached operator token is removed too, and that is not tidiness: the old
    token is signed by the key being deleted, so leaving it in place converts
    cabal#92's Mode A into its Mode B (`No key for kid`) — the exact cascade
    that made the original incident take two rounds of surgery.
    """
    db_path = sqlite_path_from_url(db_url)
    if not db_path.exists():
        raise ResetError(f"no database at {db_path}")

    backup_path = backup_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        secrets_before = _count(conn, _SECRETS)
        signing_deleted = 0
        if _table_exists(conn, _SIGNING_KEYS):
            signing_deleted = conn.execute(f"DELETE FROM {_SIGNING_KEYS}").rowcount
        secrets_purged = 0
        if purge_secrets and _table_exists(conn, _SECRETS):
            secrets_purged = conn.execute(f"DELETE FROM {_SECRETS}").rowcount
        conn.commit()
    finally:
        conn.close()

    token_removed = False
    if token_path is not None and token_path.exists():
        token_path.unlink()
        token_removed = True

    report = ResetReport(
        db_path=db_path,
        backup_path=backup_path,
        signing_keys_deleted=signing_deleted,
        secrets_purged=secrets_purged,
        secrets_remaining=secrets_before - secrets_purged,
        token_removed=token_removed,
    )
    log.warning(
        "auth.reset",
        db=str(db_path),
        backup=str(backup_path),
        signing_keys_deleted=signing_deleted,
        secrets_purged=secrets_purged,
        secrets_remaining=report.secrets_remaining,
    )
    return report
