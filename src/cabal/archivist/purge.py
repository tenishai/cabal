"""Hard-delete every version of an archivist entry (#351).

For content the archivist must not hold at all: personal data, a leaked
secret. Retiring an entry that is merely no longer true is a supersede with
a tombstone, through ``archivist.publish``; this module is for the cases
where the old row's text must leave the database.

Admin-only by construction: no handler or HTTP route imports it. It runs as
``cbl archivist purge`` on the host that holds the database file.

The FTS index is contentless (``content=''``), so its rows can only be
removed with the FTS5 ``'delete'`` command and the same column values that
were indexed. ``optimize`` then merges the index segments so the deleted
tokens leave the b-tree, and ``VACUUM`` rewrites the file so the freed pages
do not keep the text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from cabal.archivist.schema import connect

logger = logging.getLogger(__name__)

# The live server may hold a read open; wait for it rather than fail.
BUSY_TIMEOUT_MS = 30_000


@dataclass(slots=True)
class PurgeEntry:
    """One row a purge removes. Never carries the entry's text."""

    id: str
    entry_type: str
    status: str
    content_hash: str
    created_at: str


@dataclass(slots=True)
class PurgeResult:
    resource_name: str
    applied: bool
    entries: list[PurgeEntry] = field(default_factory=list)
    relations: int = 0


def purge(db_path: Path, resource_name: str, *, apply: bool = False) -> PurgeResult:
    """Find every version of ``resource_name``, whatever its status or type.

    With ``apply=False`` (the default) nothing changes and the result lists
    what would be removed. With ``apply=True`` the entries, their FTS rows and
    every relation touching them are deleted in one write transaction, then
    the index is optimized and the file vacuumed.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"no archivist database at {db_path}")
    conn = connect(db_path)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT rowid, * FROM memory_entries WHERE resource_name = ? ORDER BY created_at",
            (resource_name,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        relations = 0
        if ids:
            relations = conn.execute(
                f"SELECT COUNT(*) FROM memory_relations "
                f"WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
                ids + ids,
            ).fetchone()[0]
        result = PurgeResult(
            resource_name=resource_name,
            applied=apply,
            entries=[
                PurgeEntry(
                    id=r["id"],
                    entry_type=r["entry_type"],
                    status=r["status"],
                    content_hash=r["content_hash"],
                    created_at=r["created_at"],
                )
                for r in rows
            ],
            relations=relations,
        )
        logger.debug(
            "archivist purge %s: resource=%r entries=%d relations=%d",
            "apply" if apply else "dry-run",
            resource_name,
            len(ids),
            relations,
        )
        if not apply:
            conn.close()
            return result

        for r in rows:
            # The values must match what _insert indexed, or the delete
            # removes the wrong tokens and corrupts the index.
            conn.execute(
                """INSERT INTO memory_entries_fts (
                    memory_entries_fts, rowid, resource_name, summary, details, gotchas, tags
                ) VALUES ('delete', ?, ?, ?, ?, ?, ?)""",
                (
                    r["rowid"],
                    r["resource_name"],
                    r["summary"],
                    r["details"] or "",
                    r["gotchas"] or "",
                    r["tags"],
                ),
            )
        if ids:
            conn.execute(
                f"DELETE FROM memory_relations WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
                ids + ids,
            )
            conn.execute(f"DELETE FROM memory_entries WHERE id IN ({marks})", ids)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise

    # Runs even when nothing matched, so re-running an apply that failed here
    # finishes the cleanup.
    compact(conn)
    logger.info(
        "archivist purge: removed resource=%r entries=%d relations=%d",
        resource_name,
        len(ids),
        relations,
    )
    return result


def compact(conn) -> None:
    """Merge the FTS segments so deleted tokens leave the b-tree, then VACUUM so
    freed pages do not keep the text. Closes `conn`. Autocommit: VACUUM refuses
    to run in a transaction. Shared with erase (cabal#410)."""
    conn.isolation_level = None
    try:
        conn.execute("INSERT INTO memory_entries_fts(memory_entries_fts) VALUES ('optimize')")
        conn.execute("INSERT INTO memory_entries_fts(memory_entries_fts) VALUES ('integrity-check')")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
