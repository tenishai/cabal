"""Memory store Protocol — the persona-facing memory interface.

The store is abstract so we can plug in techne (current), graphiti (future),
or a no-op stub (tests). Personas don't know which backend they're talking
to — they just ask `recall(query)` and `commit(facts)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A single memory match for a query."""

    content: str
    type: str  # knowledge type: fact|decision|pattern|evaluation|procedure
    confidence: str  # low|medium|high
    tags: str = ""
    score: float | None = None


@runtime_checkable
class MemoryStore(Protocol):
    """Persona-facing memory operations."""

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        """Return memories relevant to `query`."""
        ...

    def commit(
        self,
        content: str,
        *,
        type: str = "fact",
        tags: str = "",
        confidence: str = "medium",
    ) -> None:
        """Persist a new memory."""
        ...


class NoOpMemoryStore:
    """Returns nothing; persists nothing. Default for personas with no store."""

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        return []

    def commit(self, content: str, **kwargs: Any) -> None:
        return None
