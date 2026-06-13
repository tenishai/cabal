"""SourceAdapter protocol — the ingest contract for Librarian sources.

Each adapter (filesystem, github, confluence, jira, ...) implements the
same async iterator surface so the Librarian's ingest loop is source-
agnostic. New adapters drop in by implementing SourceAdapter and either
registering via entry-point (cabal.librarian.adapters) or being passed
explicitly to the LibrarianStore at construction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

__all__ = ["DocumentRef", "Document", "SourceHealth", "SourceAdapter"]


@dataclass(slots=True, frozen=True)
class DocumentRef:
    """A pointer to one document in a source. Cheap to enumerate."""

    uri: str  # `librarian://<source-name>/<rel-path-or-id>`
    title: str
    last_modified: datetime
    content_hash: str | None = None  # When the source exposes one cheaply


@dataclass(slots=True)
class Document:
    """The full content + metadata for a document."""

    ref: DocumentRef
    content: str  # Plain-text extracted body
    kind: str = "doc"  # runbook / process / adr / ticket / readme / api-spec / doc
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceHealth:
    """Adapter health probe result."""

    healthy: bool
    detail: str = ""


@runtime_checkable
class SourceAdapter(Protocol):
    """Each source type implements this Protocol.

    Adapters are constructed with their source-specific config (paths,
    credentials, glob filters). Ingest is pull-based: the Librarian
    iterates `list_documents()` and then `fetch_document(ref)` per ref
    that's new or changed.
    """

    name: str  # The source's logical name (e.g. "runbooks", "eng-wiki")

    async def authenticate(self, credentials: dict[str, str]) -> None:
        """Apply credentials. Filesystem adapters are a no-op."""
        ...

    def list_documents(self) -> AsyncIterator[DocumentRef]:
        """Stream every document available from this source."""
        ...

    async def fetch_document(self, ref: DocumentRef) -> Document:
        """Materialize the document body + metadata for one ref."""
        ...

    async def healthcheck(self) -> SourceHealth:
        """Quick probe of the source. Used for status reporting."""
        ...
