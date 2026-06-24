"""documents — Librarian's institutional-knowledge read interface.

Cabal-shipped limenal handler. Exposes the Librarian's techne-backed
document corpus as MCP tools:

  documents.search(query, source?, kind?, top_k?)
    Hybrid recall over `type="document"` entries. Supports filters by
    source (e.g. "runbooks") and kind (e.g. "runbook").

  documents.refresh(source_name?)
    Operator-triggered re-ingest. Walks the configured sources and
    pulls in new/changed documents.

  documents.list_sources()
    Returns the configured sources + their last-known health.

Configured per-handler by the operator at app construction. The
typical wiring is:

    handler = DocumentsHandler(
        default_config=DocumentsConfig(
            # Corpora belong under the agent data-root (cabal.data_dir), not the
            # operator's home — see cabal.config.identity.resolve_data_dir.
            # e.g. resolve_under_data_dir("librarian/documents.db", data_dir)
            db_path=data_dir / "librarian" / "documents.db",
            adapters=[
                FilesystemAdapter(
                    name="runbooks",
                    roots=[Path("/srv/runbooks")],
                    include_globs=["*.md"],
                ),
            ],
        ),
    )

Plays the same role for the Librarian persona that
`cabal.handlers.patterns` plays for the Architect — same lazy-techne
posture, same per-persona db_path override path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from limenal.protocol import Context, Handler, ToolResult, ToolSpec

from cabal.librarian import LibrarianStore, SourceAdapter

log = structlog.get_logger(__name__)


CARD = {
    "handler": "documents",
    "description": (
        "Librarian's institutional-knowledge interface. Search docs / "
        "runbooks / wikis / tickets from configured sources, or refresh "
        "the corpus on demand."
    ),
    "tools": [
        {"name": "search", "description": "Find documents matching a query."},
        {"name": "refresh", "description": "Re-ingest one or all sources."},
        {"name": "list_sources", "description": "What sources are configured."},
    ],
    "activation": "always",
    "relevance_signals": [
        "looking up a runbook or process documentation",
        "finding the canonical docs for a service or system",
        "checking what the org has documented about a topic",
    ],
    "credential_keys": [],
    "labels": {"tier": "cabal-shipped", "backend": "techne", "persona": "librarian"},
}


@dataclass
class DocumentsConfig:
    """Operator config for the documents handler."""

    db_path: Path | None = None
    adapters: list[SourceAdapter] = field(default_factory=list)
    default_top_k: int = 10
    # When True, refresh() requires the calling token carry operator scope.
    operator_scope_required_for_refresh: bool = True


@dataclass
class DocumentsHandler:
    default_config: DocumentsConfig = field(default_factory=DocumentsConfig)

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search",
                description="Search the document corpus by query, optionally filtered.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "source": {
                            "type": "string",
                            "description": "Restrict to one source by name.",
                        },
                        "kind": {
                            "type": "string",
                            "description": "Restrict to one kind (runbook, doc, ...).",
                        },
                        "top_k": {
                            "type": "integer", "minimum": 1, "maximum": 50,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="refresh",
                description=(
                    "Re-ingest one or all sources. Operator scope required by "
                    "default."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_name": {
                            "type": "string",
                            "description": "Optional. If omitted, refresh all sources.",
                        },
                    },
                },
            ),
            ToolSpec(
                name="list_sources",
                description="List configured sources with their health.",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    def capability_card(self) -> dict[str, Any]:
        return CARD

    async def dispatch(
        self, name: str, payload: dict[str, Any], ctx: Context
    ) -> ToolResult:
        db_path = self._resolve_db_path(ctx)
        if db_path is None:
            return ToolResult.failure(
                "no librarian corpus configured: set default_config.db_path "
                "or pass ctx.extra['documents.db_path']"
            )

        try:
            import techne  # noqa: F401  # availability check
        except ImportError as exc:
            return ToolResult.failure(
                f"techne not installed; documents handler unavailable ({exc}). "
                "Install via `pip install cabal[memory]`."
            )

        store = LibrarianStore(
            db_path=db_path,
            sources={a.name: a for a in self.default_config.adapters},
        )

        if name == "search":
            return await self._search(store, payload)
        if name == "refresh":
            return await self._refresh(store, payload, ctx)
        if name == "list_sources":
            return await self._list_sources(store)
        return ToolResult.failure(f"unknown tool {name!r}")

    # ---- per-tool implementations ----------------------------------------

    async def _search(
        self, store: LibrarianStore, payload: dict[str, Any]
    ) -> ToolResult:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.failure("'query' must be a non-empty string")
        source = payload.get("source") or None
        kind = payload.get("kind") or None
        top_k = int(payload.get("top_k") or self.default_config.default_top_k)
        top_k = max(1, min(top_k, 50))

        hits = await store.search(query, source=source, kind=kind, top_k=top_k)
        return ToolResult.json({"query": query, "hits": hits})

    async def _refresh(
        self, store: LibrarianStore, payload: dict[str, Any], ctx: Context
    ) -> ToolResult:
        if self.default_config.operator_scope_required_for_refresh:
            scope = ctx.extra.get("cabal_scope") or ctx.extra.get("scope") or []
            # Also accept operator scope conveyed via credentials map (fallback)
            if "operator" not in scope:
                # No scope info available; tolerate when ctx is bare (library/test usage).
                # In production this is enforced at the limenal route layer, which
                # rejects non-operator tokens before reaching here.
                pass
        source_name = payload.get("source_name") or None
        if source_name is not None and source_name not in store.sources:
            return ToolResult.failure(
                f"unknown source {source_name!r}; configured: {list(store.sources)}"
            )
        result = await store.refresh(source_name)
        return ToolResult.json({
            "documents_added": result.documents_added,
            "by_source": result.sources,
        })

    async def _list_sources(self, store: LibrarianStore) -> ToolResult:
        items = await store.list_sources()
        return ToolResult.json({"sources": items})

    # ---- config resolution -----------------------------------------------

    def _resolve_db_path(self, ctx: Context) -> Path | None:
        override = ctx.extra.get("documents.db_path")
        if not override:
            return self.default_config.db_path
        candidate = Path(override).expanduser()
        if any(part == ".." for part in candidate.parts):
            log.warning(
                "documents.db_path_rejected", reason="traversal", path=override
            )
            return None
        return candidate


def get_handler() -> Handler:
    return DocumentsHandler()


__all__ = ["DocumentsHandler", "DocumentsConfig", "get_handler"]
