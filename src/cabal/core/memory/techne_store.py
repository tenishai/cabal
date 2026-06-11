"""techne-backed MemoryStore.

Wraps techne.search + techne.knowledge so personas can call
`recall(query)` / `commit(content)` without knowing techne exists.

techne's knowledge types: fact | decision | pattern | evaluation | procedure
techne's confidence levels: low | medium | high
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from cabal.core.memory.store import MemoryHit

_FTS5_SPECIALS = re.compile(r"""[^\w\s\-]""")

# Tokens we drop before issuing the OR query: too-short, FTS5 keywords,
# or stop-words that match nothing useful.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "to", "for", "on", "at",
    "do", "does", "did", "is", "are", "was", "were", "be", "been",
    "i", "you", "he", "she", "it", "we", "they", "me", "my", "your",
    "what", "who", "when", "where", "why", "how", "which",
    "near",  # FTS5 keyword
    "not",   # FTS5 keyword
}


def _sanitize_query(query: str) -> str:
    """Turn a natural-language query into an FTS5-friendly OR clause.

    Strips punctuation, drops stopwords, joins the rest with `OR`. An
    empty result string means "no usable terms" and recall returns no hits.
    """
    cleaned = _FTS5_SPECIALS.sub(" ", query).lower()
    tokens = [t for t in cleaned.split() if len(t) > 1 and t not in _STOPWORDS]
    return " OR ".join(tokens)

log = structlog.get_logger(__name__)


@dataclass
class TechneMemoryStore:
    """MemoryStore backed by a techne SQLite database."""

    db_path: Path
    use_vectors: bool = False  # vectors require backfill; off by default

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        # Lazy import so the dep stays soft.
        import techne

        cleaned = _sanitize_query(query)
        if not cleaned:
            return []
        try:
            results = techne.search.search(
                self.db_path,
                cleaned,
                table="knowledge",
                limit=limit,
                use_vectors=self.use_vectors,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("memory.recall_failed", error=str(exc))
            return []

        hits: list[MemoryHit] = []
        for r in results:
            hits.append(
                MemoryHit(
                    content=r.get("content", ""),
                    type=r.get("knowledge_type") or r.get("type", "fact"),
                    confidence=r.get("confidence", "medium"),
                    tags=r.get("tags", "") or "",
                    score=r.get("rank") if "rank" in r else None,
                )
            )
        return hits

    def commit(
        self,
        content: str,
        *,
        type: str = "fact",
        tags: str = "",
        confidence: str = "medium",
        **_extra: Any,
    ) -> None:
        import techne

        try:
            techne.knowledge.store(
                self.db_path,
                content=content,
                type=type,
                tags=tags,
                confidence=confidence,
                source="cabal",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("memory.commit_failed", error=str(exc))
