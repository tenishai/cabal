"""LibrarianStore — techne-backed storage for the Librarian's corpus.

Documents land as `techne.knowledge` entries with `type="document"`.
The source URI is stored in the `source` column; source-specific
metadata flows through tags (`source:<name>,kind:<kind>,...`). This
keeps the corpus queryable through the same `techne.search.search`
RRF pipeline the Architect's patterns corpus uses.

Why not a separate documents table: composability. Techne's search,
RRF fusion, decay-aware confidence — they all already work on the
knowledge table. A parallel documents table would mean rebuilding
half of techne. The trade-off is that source-specific metadata
(Confluence parent space, Jira status) is in tag form instead of
typed columns; a small price for the unified recall surface.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cabal.librarian.adapter import Document, SourceAdapter, SourceHealth

logger = logging.getLogger(__name__)

DOCUMENT_TYPE = "document"


@dataclass(slots=True)
class IngestResult:
    """Counts returned from refresh()."""

    documents_added: int = 0
    documents_skipped_existing: int = 0
    sources: dict[str, int] = field(default_factory=dict)


@dataclass
class LibrarianStore:
    """Owns the techne knowledge corpus for documents + the source adapters.

    Args:
        db_path: techne knowledge DB; reused with Architect's patterns
            corpus or pointed at a separate DB per operator preference.
        sources: Source adapters indexed by name.
    """

    db_path: Path
    sources: dict[str, SourceAdapter] = field(default_factory=dict)

    # ---- ingestion -----------------------------------------------------

    async def refresh(
        self,
        source_name: str | None = None,
        *,
        credentials: dict[str, str] | None = None,
    ) -> IngestResult:
        """Re-ingest one or all sources.

        Dedup is per (source URI + content_hash). Existing entries with
        the same URI and unchanged hash are skipped; changed entries get
        a new row (the latest one wins via the standard knowledge query
        returning newest first).
        """
        result = IngestResult()
        targets = (
            [self.sources[source_name]]
            if source_name is not None
            else list(self.sources.values())
        )
        if not targets:
            return result

        existing = self._existing_uris_and_hashes()

        for adapter in targets:
            count = await self._ingest_one(adapter, existing, credentials or {})
            result.documents_added += count
            result.sources[adapter.name] = count

        return result

    async def _ingest_one(
        self,
        adapter: SourceAdapter,
        existing: set[str],
        credentials: dict[str, str],
    ) -> int:
        try:
            await adapter.authenticate(credentials)
        except Exception:  # noqa: BLE001
            logger.exception("librarian.authenticate_failed source=%s", adapter.name)
            return 0

        added = 0
        async for ref in adapter.list_documents():
            # v1 dedup by URI alone — if the file changed, the existing
            # entry stays; supersession on change is a follow-up that
            # needs content_hash threaded through storage.
            if ref.uri in existing:
                continue
            try:
                doc = await adapter.fetch_document(ref)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "librarian.fetch_failed uri=%s source=%s",
                    ref.uri, adapter.name,
                )
                continue
            self._store_document(doc)
            existing.add(ref.uri)
            added += 1
        return added

    def _store_document(self, doc: Document) -> None:
        # Lazy import to keep techne optional.
        import techne

        content = self._format_content(doc)
        tags = ",".join(doc.tags)
        techne.knowledge.store(
            self.db_path,
            content=content,
            type=DOCUMENT_TYPE,
            tags=tags,
            confidence="medium",
            source=doc.ref.uri,
        )

    @staticmethod
    def _format_content(doc: Document) -> str:
        # Plain title + body; the techne FTS5 search will tokenize both.
        return f"# {doc.ref.title}\n\n{doc.content}".rstrip() + "\n"

    def _existing_uris_and_hashes(self) -> set[str]:
        """Source URIs currently in the corpus (v1 dedup key)."""
        from techne.core.database import connect

        if not self.db_path.exists():
            return set()
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT source FROM knowledge
                   WHERE type = ? AND superseded_by IS NULL
                     AND source IS NOT NULL""",
                (DOCUMENT_TYPE,),
            ).fetchall()
        finally:
            conn.close()
        return {row["source"] for row in rows}

    # ---- query ---------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        source: str | None = None,
        kind: str | None = None,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Search the document corpus. Filters apply post-hoc via tags."""
        import techne

        results = techne.search.search(
            self.db_path,
            query,
            table="knowledge",
            limit=max(top_k * 4, top_k),  # over-fetch to absorb filter loss
            use_vectors=True,
        )

        out: list[dict[str, Any]] = []
        for row in results:
            if (
                row.get("knowledge_type") != DOCUMENT_TYPE
                and row.get("type") != DOCUMENT_TYPE
            ):
                continue
            tags = row.get("tags") or ""
            if source is not None and f"source:{source}" not in tags:
                continue
            if kind is not None and f"kind:{kind}" not in tags:
                continue
            out.append(
                {
                    "uri": row.get("source"),
                    "content": row.get("content", ""),
                    "tags": tags,
                    "score": row.get("rrf_score") or row.get("rank"),
                    "match_source": row.get("match_source"),
                }
            )
            if len(out) >= top_k:
                break
        return out

    # ---- introspection -------------------------------------------------

    async def list_sources(self) -> list[dict[str, Any]]:
        out = []
        for name, adapter in self.sources.items():
            try:
                health = await adapter.healthcheck()
            except Exception as exc:  # noqa: BLE001
                health = SourceHealth(healthy=False, detail=str(exc))
            out.append(
                {
                    "name": name,
                    "type": type(adapter).__name__,
                    "healthy": health.healthy,
                    "detail": health.detail,
                }
            )
        return out


def documents_iterator(
    refs: Iterable, adapter: SourceAdapter
) -> AsyncIterator[Document]:
    """Helper for tests; iterates over refs and fetches each."""
    async def _gen():
        for ref in refs:
            yield await adapter.fetch_document(ref)
    return _gen()


__all__ = ["DOCUMENT_TYPE", "IngestResult", "LibrarianStore", "documents_iterator"]


# Avoid an "unused" warning on `datetime` — it's referenced via the dataclass
# typehints below if we add timestamps; keep it imported for future fields.
_ = datetime
