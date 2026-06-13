"""Librarian — multi-source mirror of institutional knowledge.

The Librarian persona owns a queryable mirror of pre-existing
authoritative documentation (org docs, wikis, runbooks, tickets). Each
source adapter knows how to enumerate and fetch its content; the
Librarian orchestrates ingest into the techne knowledge corpus under
`type="document"`, where every other persona can query it via the
`documents` MCP handler.

Ships with the filesystem adapter today. Future adapters (GitHub repo
walks, Confluence REST, Jira REST) drop in by implementing the
SourceAdapter protocol and registering via entry-point — same shape
as limenal handlers.

Read interface: cabal.handlers.documents (the MCP handler).
"""

from cabal.librarian.adapter import (
    Document,
    DocumentRef,
    SourceAdapter,
    SourceHealth,
)
from cabal.librarian.adapters.filesystem import FilesystemAdapter
from cabal.librarian.store import LibrarianStore

__all__ = [
    "Document",
    "DocumentRef",
    "FilesystemAdapter",
    "LibrarianStore",
    "SourceAdapter",
    "SourceHealth",
]
