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

    outcome: str  # "new" | "duplicate_l0" | "duplicate_l1" | "related" | "superseded"
    #               | "refused_erased"
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
        derived_from: list[str] | None = None,
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
                              With supersede=True a duplicate still retires
                              the OTHER active entries in scope, keeping the
                              matched entry as the current one (#363);
                              related_ids holds the ids it retired.
              related       — entry was written AND linked to overlapping entries
                              via RELATED edges (Tier 1).
              superseded    — entry was written AND it retired prior same-resource
                              active entries (supersede=True); related_ids holds
                              the now-superseded ids.
              refused_erased — nothing written: the content, the entry's refs or
                              a ``derived_from`` source was erased (cabal#410).
                              entry_id is empty; related_ids holds the erased
                              references that matched.

            derived_from: the sources this entry was made from, recorded as
                lineage so erasing any of them reaches this entry (cabal#410).
                The entry's ``refs`` are recorded too.
        """
        if supersede_scope not in SUPERSEDE_SCOPES:
            raise ValueError(
                f"supersede_scope must be one of {list(SUPERSEDE_SCOPES)}; got {supersede_scope!r}"
            )

        # ONE write transaction from the dedup check through the insert and any
        # supersede (#358). The check, the insert and the retirements used to run
        # on separate connections, so identical publishes racing from two writers
        # both saw no duplicate and both inserted: 8 processes x 30 rounds left
        # 30 hashes with more than one active row. BEGIN IMMEDIATE takes the
        # write lock before the first read, so a second writer waits and then
        # sees the first one's row.
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            sources = _dedupe([*(derived_from or []), *entry.refs])
            if not entry.content_hash:
                entry.content_hash = entry.compute_hash()
            erased = _erased_refs(conn, [entry.content_hash, entry.text_hash(), *sources])
            if erased:
                conn.commit()
                return PublishResult(outcome="refused_erased", entry_id="", related_ids=erased)
            result = self._dedup.check(entry, conn=conn)

            if result.outcome in ("duplicate_l0", "duplicate_l1"):
                assert result.entry_id is not None
                # A new source for content already held still joins its lineage,
                # so erasing that source reaches the entry (cabal#410).
                _insert_lineage(conn, result.entry_id, sources)
                retired = (
                    self._supersede_from_match(result.entry_id, supersede_scope, conn)
                    if supersede
                    else []
                )
                conn.commit()
                return PublishResult(
                    outcome=result.outcome,
                    entry_id=result.entry_id,
                    related_ids=retired,
                )

            # Dedup's Tier-1 overlap only sees same-type priors, so with resource
            # scope a different-type prior arrives as "new" and still has to be
            # looked up here.
            if supersede and (result.outcome == "related" or supersede_scope == "resource"):
                # Insert the new entry AND retire EVERY active prior in scope in ONE
                # transaction (#27) — never leaving two 'current' entries or a
                # half-applied supersession, and not capped by the dedup LIMIT-5
                # sample.
                self._insert(entry, conn=conn)
                _insert_lineage(conn, entry.id, sources)
                superseded_ids = self._supersede_all(entry, conn, scope=supersede_scope)
                conn.commit()
                self._dedup.record_new(entry)
                return PublishResult(
                    outcome="superseded" if superseded_ids else "new",
                    entry_id=entry.id,
                    related_ids=superseded_ids,
                )

            self._insert(entry, conn=conn)
            _insert_lineage(conn, entry.id, sources)
            if result.outcome == "related" and result.related_ids:
                edges = make_related_edges(entry.id, result.related_ids)
                self._insert_relations(edges, conn=conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._dedup.record_new(entry)

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
                    written_by, origin_archivist, origin_cabal, origin_agent,
                    origin_role, access, access_count, created_at, updated_at
                ) VALUES (
                    :id, :resource_name, :summary, :entry_type, :tags,
                    :details, :gotchas, :commands, :refs, :importance,
                    :source_thread, :content_hash, :status, :processing_state,
                    :written_by, :origin_archivist, :origin_cabal, :origin_agent,
                    :origin_role, :access, :access_count, :created_at, :updated_at
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

    def _insert_relations(
        self, relations: list[Relation], conn: sqlite3.Connection | None = None
    ) -> None:
        """Insert edges. With `conn` the caller owns the transaction."""
        if not relations:
            return
        owned = conn is None
        if conn is None:
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
            if owned:
                conn.commit()
        finally:
            if owned:
                conn.close()

    def _supersede_from_match(
        self, match_id: str, scope: SupersedeScope, conn: sqlite3.Connection
    ) -> list[str]:
        """A supersede that exactly matches an active entry still means "this
        is the current truth" (#363): retire the other active entries in
        `scope`, with the matched entry as the survivor. The active check and
        the retirements share one write transaction. Dedup reports only active
        matches (#353), so a match that is no longer active here was retired by
        another writer after the check; it retires nothing. Runs on the caller's
        connection, inside publish's write transaction (#358). Returns the
        retired ids."""
        row = conn.execute(
            "SELECT * FROM memory_entries WHERE id=? AND status='active'", (match_id,)
        ).fetchone()
        if row is None:
            return []
        return self._supersede_all(Entry.from_row(dict(row)), conn, scope=scope)

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
        caller_cabal: str | None = None,
        caller_role: str | None = None,
    ) -> list[Entry]:
        """FTS5 search across resource_name + summary + details + gotchas + tags.

        Optionally filtered by `entry_type` (exact match) and `tags`
        (every supplied tag must appear in the row's tags list).

        Ranked by origin first when the caller names its cabal or role
        (cabal#410): the caller's own cabal, then the same role in other
        cabals, then everything else, with bm25 inside each group. Nothing is
        hidden by origin; it only orders.
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
        # Tags filter in SQL, before the LIMIT (#362). Filtering in Python after an
        # over-fetch of top_k*4 returned nothing when the matching rows ranked
        # below that many rows with other tags.
        for tag in tags or []:
            sql.append("  AND EXISTS (SELECT 1 FROM json_each(e.tags) WHERE value = ?)")
            params.append(tag)
        order = ["bm25(memory_entries_fts)"]
        if caller_cabal or caller_role:
            order.insert(
                0,
                "CASE WHEN ? <> '' AND e.origin_cabal = ? THEN 0 "
                "WHEN ? <> '' AND e.origin_role = ? THEN 1 ELSE 2 END",
            )
            params.extend([caller_cabal or "", caller_cabal or "", caller_role or "", caller_role or ""])
        sql.append("ORDER BY " + ", ".join(order) + " LIMIT ?")
        params.append(top_k)

        conn = connect(self.db_path)
        try:
            rows = conn.execute("\n".join(sql), params).fetchall()
        finally:
            conn.close()

        return [Entry.from_row(dict(raw_row)) for raw_row in rows]

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

    def superseded_by(self, entry_id: str) -> list[str]:
        """Ids of the entries that superseded this one: the sources of
        SUPERSEDES edges pointing at it, oldest first. Empty for an entry
        nothing has replaced. One hop only; a successor that was itself
        superseded has its own `superseded_by`."""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT src_id FROM memory_relations
                   WHERE dst_id = ? AND relation_type = 'SUPERSEDES'
                   ORDER BY created_at, src_id""",
                (entry_id,),
            ).fetchall()
        finally:
            conn.close()
        return [r["src_id"] for r in rows]

    def lineage(self, entry_id: str) -> list[str]:
        """The sources an entry was derived from, as recorded on publish."""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT source_ref FROM memory_lineage WHERE derived_id = ? ORDER BY source_ref",
                (entry_id,),
            ).fetchall()
        finally:
            conn.close()
        return [r["source_ref"] for r in rows]

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
            "ranking": ["caller_cabal", "caller_role"],
            "access_labels": ["internal", "public"],
        }


# ---- internals ------------------------------------------------------------


def _dedupe(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        if v and v not in seen:
            seen[v] = None
    return list(seen)


def _erased_refs(conn: sqlite3.Connection, refs: list[str]) -> list[str]:
    """The members of `refs` (content hashes, entry ids or outside references)
    that have been erased."""
    refs = _dedupe(refs)
    if not refs:
        return []
    marks = ",".join("?" * len(refs))
    rows = conn.execute(
        f"SELECT ref FROM memory_erasures WHERE ref IN ({marks}) ORDER BY ref", refs
    ).fetchall()
    return [r["ref"] for r in rows]


def _insert_lineage(conn: sqlite3.Connection, derived_id: str, sources: list[str]) -> None:
    now = datetime.now(UTC).isoformat()
    for ref in sources:
        conn.execute(
            "INSERT OR IGNORE INTO memory_lineage (derived_id, source_ref, created_at) "
            "VALUES (?, ?, ?)",
            (derived_id, ref, now),
        )


def _sanitize_fts(query: str) -> str:
    """Quote every whitespace-separated token as an FTS5 string.

    FTS5 barewords allow only letters, digits, ``_`` and non-ASCII, so
    ``mash1.local`` or ``host?`` left bare is a syntax error (cabal#356).
    Quoted, each token is plain text: the tokenizer splits ``mash1.local``
    into the phrase ``mash1 local``, and ``AND``/``OR``/``NOT``, ``*``,
    ``^``, ``:`` and ``{}`` lose their operator meaning. Tokens are still
    implicitly AND-ed, as bare words were.

    Control characters (NUL included) become spaces, because FTS5's parser stops
    at a NUL inside a quoted token and raises "unterminated string", and a lone
    surrogate becomes U+FFFD, because sqlite3 can't encode one to bind it
    (#365). A surrogate pair held as two code points is joined into one
    character.
    """
    return " ".join('"' + t.replace('"', '""') + '"' for t in _clean_query(query).split())


def _clean_query(query: str) -> str:
    text = query.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text)
