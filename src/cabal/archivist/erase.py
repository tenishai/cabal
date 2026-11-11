"""Erase an entry, or a source, and every entry derived from it (cabal#410).

When a publisher withdraws something, every copy made from it has to go too,
including an entry that was merged from several sources. Publish records
lineage (``memory_lineage``: derived entry -> source reference), so an erase
walks it: the named entry or outside reference, every entry derived from it,
every entry derived from those, and so on.

Erasure is a state, not a delete. Each reached entry keeps its row with
``status='erased'``, its id, resource_name and content_hash, and loses its
text: summary, details, gotchas, commands, refs and tags. Its lineage rows stay
on purpose: they hold identifiers, not text, the walk and ``other_sources``
need them, and ``archivist.get`` still reports them as ``derived_from``. The reference, each
entry id and each content hash go into ``memory_erasures``, which publish and
the git-corpus sync both check, so neither can bring the text back. A hash
of the text alone is recorded too, so the same words under other refs, tags or
a new resource_name are refused as well.

A merged entry is erased, not re-derived: re-deriving it from its remaining
sources needs the writer's consolidation step. The result names those entries
so the writer can republish from what is left.

Erase does not follow SUPERSEDES edges: a successor is a correction, not a
copy. The result lists each reached entry's unreached successors so an operator
can see them, and use purge on that resource_name when every version must go.

Neither erase nor purge reaches git history. In the git corpus the erased text
leaves the tip, but ``git log -p`` on the corpus repo still shows it; removing
it there needs a history rewrite of that repo.

Admin-only, like purge: no handler or HTTP route imports it. It runs as
``cbl archivist erase`` on the host that holds the database file. For text
that must leave the file with no trace at all, use purge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cabal.archivist.models import text_hash
from cabal.archivist.purge import BUSY_TIMEOUT_MS, compact
from cabal.archivist.schema import connect, init_db

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ErasedEntry:
    """One entry an erase reaches. Never carries the entry's text."""

    id: str
    resource_name: str
    status: str  # status before the erase
    via: str  # the reference that reached it: the erased ref, or a source entry id
    other_sources: list[str] = field(default_factory=list)  # lineage left after the erase
    # Entries that SUPERSEDE this one and are not reached. Not erased, since a
    # correction is not a copy; listed so an operator who wants every version
    # gone can use purge on purpose.
    successors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EraseResult:
    ref: str
    applied: bool
    entries: list[ErasedEntry] = field(default_factory=list)


def erase(db_path: Path, ref: str, *, reason: str = "", apply: bool = False) -> EraseResult:
    """Find what erasing ``ref`` reaches: an entry id, or an outside source
    reference such as ``techne:poe@dimartin:knowledge/4255``.

    With ``apply=False`` (the default) nothing changes. With ``apply=True`` the
    reached entries are blanked and marked erased, and the references recorded,
    in one write transaction; then the index is optimized and the file vacuumed.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"no archivist database at {db_path}")
    init_db(db_path).close()  # an older file gains the lineage and erasure tables
    conn = connect(db_path)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        reached = _walk(conn, ref)
        erased_ids = {e.id for e in reached}
        for e in reached:
            rows = conn.execute(
                "SELECT source_ref FROM memory_lineage WHERE derived_id = ? ORDER BY source_ref",
                (e.id,),
            ).fetchall()
            e.other_sources = [
                r["source_ref"]
                for r in rows
                if r["source_ref"] != ref and r["source_ref"] not in erased_ids
            ]
            e.successors = [
                r["src_id"]
                for r in conn.execute(
                    "SELECT src_id FROM memory_relations WHERE dst_id = ? "
                    "AND relation_type = 'SUPERSEDES' ORDER BY src_id",
                    (e.id,),
                ).fetchall()
                if r["src_id"] not in erased_ids
            ]
        result = EraseResult(ref=ref, applied=apply, entries=reached)
        logger.debug(
            "archivist erase %s: ref=%r entries=%d",
            "apply" if apply else "dry-run",
            ref,
            len(reached),
        )
        if not apply:
            conn.close()
            return result

        now = datetime.now(UTC).isoformat()
        _record(conn, ref, "entry" if ref in erased_ids else "source", reason, now,
                ref if ref in erased_ids else "")
        for e in reached:
            row = conn.execute(
                "SELECT rowid, * FROM memory_entries WHERE id = ?", (e.id,)
            ).fetchone()
            _record(conn, row["id"], "entry", reason, now, row["id"])
            _record(conn, row["content_hash"], "content", reason, now, row["id"])
            if row["status"] == "erased":
                continue
            _record(
                conn,
                text_hash(row["summary"], row["details"], row["gotchas"]),
                "text",
                reason,
                now,
                row["id"],
            )
            # The values must match what _insert indexed, or the delete removes
            # the wrong tokens and corrupts the index.
            conn.execute(
                """INSERT INTO memory_entries_fts (
                    memory_entries_fts, rowid, resource_name, summary, details, gotchas, tags
                ) VALUES ('delete', ?, ?, ?, ?, ?, ?)""",
                (
                    row["rowid"],
                    row["resource_name"],
                    row["summary"],
                    row["details"] or "",
                    row["gotchas"] or "",
                    row["tags"],
                ),
            )
            conn.execute(
                """UPDATE memory_entries SET
                    summary = '', details = NULL, gotchas = NULL,
                    commands = '[]', refs = '[]', tags = '[]',
                    status = 'erased', updated_at = ?
                   WHERE id = ?""",
                (now, row["id"]),
            )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise

    compact(conn)
    logger.info("archivist erase: ref=%r entries=%d", ref, len(reached))
    return result


def _walk(conn, ref: str) -> list[ErasedEntry]:
    """Breadth-first over lineage from `ref`. An entry is reached once."""
    reached: dict[str, ErasedEntry] = {}
    frontier = [ref]
    seen_refs = {ref}
    while frontier:
        nxt: list[str] = []
        for r in frontier:
            hits = []
            row = conn.execute(
                "SELECT id, resource_name, status FROM memory_entries WHERE id = ?", (r,)
            ).fetchone()
            if row is not None:
                hits.append((row, ref if r == ref else r))
            for d in conn.execute(
                """SELECT e.id, e.resource_name, e.status FROM memory_lineage l
                   JOIN memory_entries e ON e.id = l.derived_id
                   WHERE l.source_ref = ? ORDER BY e.id""",
                (r,),
            ).fetchall():
                hits.append((d, r))
            for h, via in hits:
                if h["id"] in reached:
                    continue
                reached[h["id"]] = ErasedEntry(
                    id=h["id"], resource_name=h["resource_name"], status=h["status"], via=via
                )
                if h["id"] not in seen_refs:
                    seen_refs.add(h["id"])
                    nxt.append(h["id"])
        frontier = nxt
    return list(reached.values())


def _record(conn, ref: str, kind: str, reason: str, now: str, entry_id: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO memory_erasures (ref, kind, entry_id, reason, erased_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ref, kind, entry_id, reason, now),
    )
