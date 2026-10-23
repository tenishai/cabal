"""ArchivistStore — the publish + search + introspect surface.

Wraps the SQLite schema + the dedup pipeline + relation management. The
DocumentsHandler / handler layer above never touches SQLite directly;
it goes through this store.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cabal.archivist.dedup import Dedupper, make_related_edges
from cabal.archivist.models import Entry, Relation
from cabal.archivist.schema import connect, init_db

SupersedeScope = Literal["type", "resource"]
SUPERSEDE_SCOPES: tuple[str, ...] = ("type", "resource")


@dataclass(slots=True)
class PublishResult:
    """What happened to a publish() call."""

    outcome: str  # "new" | "duplicate_l0" | "duplicate_l1" | "related"
    entry_id: str
    related_ids: list[str]


class ArchivistStore:
    """Top-level facade. One per archivist deployment.

    Construction is cheap — DB connection opens per call. The dedup L0
    cache lives in the Dedupper instance for the lifetime of the store.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        # First call to init_db is harmless if the schema already exists.
        init_db(db_path).close()
        self._dedup = Dedupper(db_path)

    # ---- write path -------------------------------------------------------

    def publish(
        self,
        entry: Entry,
        *,
        supersede: bool = False,
        supersede_scope: SupersedeScope = "type",
    ) -> PublishResult:
        """Write an entry through the dedup pipeline.

        Args:
            supersede: when True and the entry overlaps existing *active* entries
                with the same (resource_name, entry_type) — the dedup ``related``
                case — treat the new entry as the current truth: write SUPERSEDES
                edges to the priors and flip them to ``status='superseded'``.
                Since ``search`` is active-only, latest-wins falls out for free.
                Deterministic, no LLM (agent-model design §5e). Default False
                preserves the RELATED-linking behavior.
            supersede_scope: which priors a supersede retires. ``"type"`` (the
                default) retires only same (resource_name, entry_type) entries,
                so one resource can hold a fact, a decision and a procedure side
                by side. ``"resource"`` retires every active entry with the same
                resource_name whatever its type, for writers that use one name
                per fact and may revise its type (#350). It has no effect when
                ``supersede`` is False, but an unknown value raises
                ``ValueError`` either way.

        Returns:
            PublishResult.outcome:
              new           — entry was written; first time we've seen this content
              duplicate_l0  — content matched an in-memory cache hit; no write
              duplicate_l1  — content matched an active DB row; no write
              related       — entry was written AND linked to overlapping entries
                              via RELATED edges (Tier 1).
              superseded    — entry was written AND it retired prior same-resource
                              active entries (supersede=True); related_ids holds
                              the now-superseded ids.
        """
        if supersede_scope not in SUPERSEDE_SCOPES:
            raise ValueError(
                f"supersede_scope must be one of {list(SUPERSEDE_SCOPES)}; got {supersede_scope!r}"
            )

        result = self._dedup.check(entry)

        if result.outcome in ("duplicate_l0", "duplicate_l1"):
            assert result.entry_id is not None
            return PublishResult(
                outcome=result.outcome,
                entry_id=result.entry_id,
                related_ids=[],
            )

        # Dedup's Tier-1 overlap only sees same-type priors, so with resource
        # scope a different-type prior arrives as "new" and still has to be
        # looked up here.
        if supersede and (result.outcome == "related" or supersede_scope == "resource"):
            # Insert the new entry AND retire EVERY active prior in scope in ONE
            # transaction (#27) — never leaving two 'current' entries or a
            # half-applied supersession, and not capped by the dedup LIMIT-5
            # sample.
            conn = connect(self.db_path)
            try:
                self._insert(entry, conn=conn)
                superseded_ids = self._supersede_all(entry, conn, scope=supersede_scope)
                conn.commit()
            finally:
                conn.close()
            self._dedup.record_new(entry)
            return PublishResult(
                outcome="superseded" if superseded_ids else "new",
                entry_id=entry.id,
                related_ids=superseded_ids,
            )

        self._insert(entry)
        self._dedup.record_new(entry)

        if result.outcome == "related" and result.related_ids:
            edges = make_related_edges(entry.id, result.related_ids)
            self._insert_relations(edges)

        return PublishResult(
            outcome=result.outcome,
            entry_id=entry.id,
            related_ids=result.related_ids,
        )

    def _insert(self, entry: Entry, conn: sqlite3.Connection | None = None) -> None:
        """Insert `entry` + its FTS row. If `conn` is given the caller owns the
        transaction (no commit/close here), so insert can be made atomic with a
        same-connection supersede."""
        owned = conn is None
        if owned:
            conn = connect(self.db_path)
        row = entry.to_row()
        try:
            conn.execute(
                """INSERT INTO memory_entries (
                    id, resource_name, summary, entry_type, tags,
                    details, gotchas, commands, refs, importance,
                    source_thread, content_hash, status, processing_state,
                    written_by, access_count, created_at, updated_at
                ) VALUES (
                    :id, :resource_name, :summary, :entry_type, :tags,
                    :details, :gotchas, :commands, :refs, :importance,
                    :source_thread, :content_hash, :status, :processing_state,
                    :written_by, :access_count, :created_at, :updated_at
                )""",
                row,
            )
            # Mirror searchable fields to the FTS index
            conn.execute(
                """INSERT INTO memory_entries_fts (
                    rowid, resource_name, summary, details, gotchas, tags
                ) VALUES (
                    (SELECT rowid FROM memory_entries WHERE id = ?),
                    ?, ?, ?, ?, ?
                )""",
                (
                    row["id"],
                    row["resource_name"],
                    row["summary"],
                    row["details"] or "",
                    row["gotchas"] or "",
                    row["tags"],
                ),
            )
            if owned:
                conn.commit()
        finally:
            if owned:
                conn.close()

    def _insert_relations(self, relations: list[Relation]) -> None:
        if not relations:
            return
        conn = connect(self.db_path)
        try:
            for rel in relations:
                conn.execute(
                    """INSERT OR IGNORE INTO memory_relations (
                        src_id, dst_id, relation_type, metadata, created_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        rel.src_id,
                        rel.dst_id,
                        rel.relation_type,
                        json.dumps(rel.metadata),
                        rel.created_at or datetime.now(UTC).isoformat(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _supersede_all(
        self, entry: Entry, conn: sqlite3.Connection, *, scope: SupersedeScope = "type"
    ) -> list[str]:
        """Within the caller's transaction (no commit here), retire EVERY active
        entry sharing `entry`'s (resource_name, entry_type) — or just its
        resource_name when `scope` is "resource" (#350) — except `entry` itself,
        flipping status to 'superseded' and recording a SUPERSEDES edge
        new -> prior. Unbounded: queries all priors directly, not the dedup
        LIMIT-5 overlap sample (#27). Returns the superseded ids."""
        now = datetime.now(UTC).isoformat()
        if scope == "resource":
            sql = (
                "SELECT id FROM memory_entries "
                "WHERE resource_name=? AND status='active' AND id != ?"
            )
            params: tuple[str, ...] = (entry.resource_name, entry.id)
        else:
            sql = (
                "SELECT id FROM memory_entries "
                "WHERE resource_name=? AND entry_type=? AND status='active' AND id != ?"
            )
            params = (entry.resource_name, entry.entry_type, entry.id)
        priors = [r["id"] for r in conn.execute(sql, params).fetchall()]
        for prior in priors:
            conn.execute(
                "UPDATE memory_entries SET status='superseded', updated_at=? "
                "WHERE id=? AND status='active'",
                (now, prior),
            )
            conn.execute(
                """INSERT OR IGNORE INTO memory_relations (
                    src_id, dst_id, relation_type, metadata, created_at
                ) VALUES (?, ?, 'SUPERSEDES', ?, ?)""",
                (entry.id, prior, json.dumps({"reason": "same_resource_supersedes"}), now),
            )
        return priors

    # ---- read path --------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        entry_type: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 10,
    ) -> list[Entry]:
        """FTS5 search across resource_name + summary + details + gotchas + tags.

        Optionally filtered by `entry_type` (exact match) and `tags`
        (every supplied tag must appear in the row's tags list).
        """
        fts_query = _sanitize_fts(query)
        if not fts_query:
            return []

        sql = [
            "SELECT e.* FROM memory_entries e",
            "JOIN memory_entries_fts f ON f.rowid = e.rowid",
            "WHERE memory_entries_fts MATCH ?",
            "  AND e.status = 'active'",
        ]
        params: list[Any] = [fts_query]
        if entry_type is not None:
            sql.append("  AND e.entry_type = ?")
            params.append(entry_type)
        sql.append("ORDER BY bm25(memory_entries_fts) LIMIT ?")
        params.append(top_k * 4 if tags else top_k)  # over-fetch when tag-filtering

        conn = connect(self.db_path)
        try:
            rows = conn.execute("\n".join(sql), params).fetchall()
        finally:
            conn.close()

        out: list[Entry] = []
        for raw_row in rows:
            entry = Entry.from_row(dict(raw_row))
            if tags is not None and not all(t in entry.tags for t in tags):
                continue
            out.append(entry)
            if len(out) >= top_k:
                break
        return out

    def get(self, entry_id: str) -> Entry | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM memory_entries WHERE id = ?", (entry_id,)).fetchone()
        finally:
            conn.close()
        return Entry.from_row(dict(row)) if row else None

    def list_related(self, entry_id: str) -> list[dict[str, Any]]:
        """Outbound edges from one entry."""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT dst_id, relation_type, metadata, created_at
                   FROM memory_relations WHERE src_id = ?""",
                (entry_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "dst_id": r["dst_id"],
                "relation_type": r["relation_type"],
                "metadata": json.loads(r["metadata"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ---- introspection ----------------------------------------------------

    def schema(self) -> dict[str, Any]:
        """Self-discovery payload — describes the corpus schema +
        counts so agents can self-orient.
        """
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_entries WHERE status = 'active'"
            ).fetchone()["n"]
            by_type = conn.execute(
                """SELECT entry_type, COUNT(*) AS n
                   FROM memory_entries WHERE status = 'active'
                   GROUP BY entry_type"""
            ).fetchall()
            sample_resources = conn.execute(
                """SELECT DISTINCT resource_name FROM memory_entries
                   WHERE status = 'active' LIMIT 10"""
            ).fetchall()
            relations_total = conn.execute("SELECT COUNT(*) AS n FROM memory_relations").fetchone()[
                "n"
            ]
        finally:
            conn.close()

        return {
            "tables": ["memory_entries", "memory_relations", "memory_entries_fts"],
            "entry_types": ["fact", "decision", "operation", "error", "procedure", "document"],
            "counts": {
                "total_active": total,
                "by_type": {row["entry_type"]: row["n"] for row in by_type},
                "relations": relations_total,
            },
            "sample_resources": [r["resource_name"] for r in sample_resources],
            "search_fields": [
                "resource_name",
                "summary",
                "details",
                "gotchas",
                "tags",
            ],
            "filters": ["entry_type", "tags"],
        }


# ---- internals ------------------------------------------------------------


_FTS_SPECIAL = '"-^*():'


def _sanitize_fts(query: str) -> str:
    """Strip FTS5-special chars; bare tokens are fine."""
    query = query.strip()
    if not query:
        return ""
    if any(c in query for c in _FTS_SPECIAL):
        tokens = [t.replace('"', '""') for t in query.split() if t.strip()]
        return " ".join(f'"{t}"' for t in tokens) if tokens else ""
    return query
