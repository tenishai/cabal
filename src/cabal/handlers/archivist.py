"""archivist — Archivist's team-consensus knowledge interface.

Cabal-shipped liminal handler. Four tools:

  archivist.publish(resource_name, summary, entry_type, ...)
    Write an entry through the dedup pipeline. Returns the entry id
    + the outcome (new / duplicate_l0 / duplicate_l1 / related).

  archivist.search(query, entry_type?, tags?, top_k?)
    FTS5 search across the team-memory corpus. Filter by entry_type
    or required tags.

  archivist.get(entry_id)
    Fetch one entry by id, with its outbound RELATED edges.

  archivist.schema()
    Return the corpus self-discovery payload — entry_type catalog,
    counts, sample resource_names, search fields, filters.

Configured at construction with an ArchivistConfig holding the
Archivist DB path. Per-persona override via
ctx.extra["archivist.db_path"] (rejects traversal). The Archivist
owns its own SQLite DB, separate from the techne knowledge corpus
the Architect / Librarian / Codex use.

Deferred from MVP (covered in cabal/archivist/__init__.py docstring):
  - Tier-2 LLM contradiction/supersession classification
  - Temporal knowledge graph (graph_nodes, graph_edges)
  - Background write-worker for async Tier-2 processing
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from liminal.protocol import Context, Handler, ToolResult, ToolSpec

from cabal.archivist import ArchivistStore, Entry

log = structlog.get_logger(__name__)


CARD = {
    "handler": "archivist",
    "description": (
        "Archivist's team-consensus knowledge interface. Publish facts/"
        "decisions/operations/errors/procedures the team has agreed on, "
        "search the corpus, fetch by id, or introspect the schema."
    ),
    "tools": [
        {"name": "publish", "description": "Write an entry through the dedup pipeline."},
        {"name": "search", "description": "Search the team-memory corpus."},
        {"name": "get", "description": "Fetch one entry by id (with edges)."},
        {"name": "schema", "description": "Self-discovery: types, counts, fields."},
    ],
    "activation": "always",
    "relevance_signals": [
        "recording a decision the team agreed on",
        "publishing an observation from an investigation",
        "checking what the team has learned about a service or system",
        "looking up why an architectural choice was made",
        "finding past incidents matching a symptom",
    ],
    "credential_keys": [],
    "labels": {
        "tier": "cabal-shipped",
        "backend": "sqlite",
        "persona": "archivist",
    },
}


_VALID_ENTRY_TYPES = (
    "fact", "decision", "operation", "error", "procedure", "document",
)


@dataclass
class ArchivistConfig:
    db_path: Path | None = None
    default_top_k: int = 10


@dataclass
class ArchivistHandler:
    default_config: ArchivistConfig = field(default_factory=ArchivistConfig)

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="publish",
                description=(
                    "Publish an entry to team memory. The dedup pipeline "
                    "(L0 LRU → L1 hash → Tier-1 resource overlap) "
                    "decides whether to write or link."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "resource_name": {
                            "type": "string",
                            "description": "Kebab-case identifier (the 'what').",
                        },
                        "summary": {"type": "string"},
                        "entry_type": {
                            "type": "string",
                            "enum": list(_VALID_ENTRY_TYPES),
                        },
                        "tags": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "details": {"type": "string"},
                        "gotchas": {
                            "type": "string",
                            "description": (
                                "Pitfalls and caveats — sparse but high-value."
                            ),
                        },
                        "commands": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "refs": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "importance": {
                            "type": "number", "minimum": 0.0, "maximum": 1.0,
                        },
                        "source_thread": {"type": "string"},
                    },
                    "required": ["resource_name", "summary", "entry_type"],
                },
            ),
            ToolSpec(
                name="search",
                description="Search the team-memory corpus.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "entry_type": {
                            "type": "string",
                            "enum": list(_VALID_ENTRY_TYPES),
                        },
                        "tags": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "top_k": {
                            "type": "integer", "minimum": 1, "maximum": 50,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="get",
                description="Fetch one entry + its outbound edges.",
                input_schema={
                    "type": "object",
                    "properties": {"entry_id": {"type": "string"}},
                    "required": ["entry_id"],
                },
            ),
            ToolSpec(
                name="schema",
                description="Return the corpus self-discovery payload.",
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
                "no archivist corpus configured: set CABAL_ARCHIVIST_DB, "
                "default_config.db_path, or pass ctx.extra['archivist.db_path']"
            )

        store = ArchivistStore(db_path)

        if name == "publish":
            return self._publish(store, payload, ctx)
        if name == "search":
            return self._search(store, payload)
        if name == "get":
            return self._get(store, payload)
        if name == "schema":
            return ToolResult.json(store.schema())
        return ToolResult.failure(f"unknown tool {name!r}")

    # ---- tools ---------------------------------------------------------

    def _publish(
        self, store: ArchivistStore, payload: dict[str, Any], ctx: Context
    ) -> ToolResult:
        for field_name in ("resource_name", "summary", "entry_type"):
            value = payload.get(field_name)
            if not isinstance(value, str) or not value.strip():
                return ToolResult.failure(
                    f"'{field_name}' must be a non-empty string"
                )

        entry_type = payload["entry_type"]
        if entry_type not in _VALID_ENTRY_TYPES:
            return ToolResult.failure(
                f"'entry_type' must be one of {list(_VALID_ENTRY_TYPES)}; "
                f"got {entry_type!r}"
            )

        entry = Entry(
            resource_name=payload["resource_name"].strip(),
            summary=payload["summary"].strip(),
            entry_type=entry_type,
            tags=_as_str_list(payload.get("tags")),
            details=payload.get("details"),
            gotchas=payload.get("gotchas"),
            commands=_as_str_list(payload.get("commands")),
            refs=_as_str_list(payload.get("refs")),
            importance=float(payload.get("importance") or 1.0),
            source_thread=payload.get("source_thread"),
            written_by=ctx.identity or "unknown",
        )

        result = store.publish(entry)
        return ToolResult.json({
            "outcome": result.outcome,
            "entry_id": result.entry_id,
            "related_ids": result.related_ids,
        })

    def _search(
        self, store: ArchivistStore, payload: dict[str, Any]
    ) -> ToolResult:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.failure("'query' must be a non-empty string")
        entry_type = payload.get("entry_type") or None
        if entry_type is not None and entry_type not in _VALID_ENTRY_TYPES:
            return ToolResult.failure(
                f"'entry_type' must be one of {list(_VALID_ENTRY_TYPES)}; "
                f"got {entry_type!r}"
            )
        tags = _as_str_list(payload.get("tags")) or None
        top_k = int(payload.get("top_k") or self.default_config.default_top_k)
        top_k = max(1, min(top_k, 50))

        hits = store.search(
            query, entry_type=entry_type, tags=tags, top_k=top_k,
        )
        return ToolResult.json({
            "query": query,
            "hits": [_entry_to_dict(e) for e in hits],
        })

    def _get(
        self, store: ArchivistStore, payload: dict[str, Any]
    ) -> ToolResult:
        entry_id = payload.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            return ToolResult.failure("'entry_id' must be a non-empty string")
        entry = store.get(entry_id)
        if entry is None:
            return ToolResult.failure(f"no entry with id {entry_id!r}")
        relations = store.list_related(entry_id)
        return ToolResult.json({
            "entry": _entry_to_dict(entry),
            "related": relations,
        })

    def _resolve_db_path(self, ctx: Context) -> Path | None:
        override = ctx.extra.get("archivist.db_path")
        if not override:
            return self.default_config.db_path
        candidate = Path(override).expanduser()
        if any(part == ".." for part in candidate.parts):
            log.warning(
                "archivist.db_path_rejected", reason="traversal", path=override,
            )
            return None
        return candidate


def get_handler() -> Handler:
    """Entry-point factory, called by liminal discovery with no arguments.

    Reads ``CABAL_ARCHIVIST_DB`` (if set and non-empty) as the served handler's fallback
    corpus path, the same convention as ``CABAL_ARCHITECT_DB`` for patterns
    (cabal#129). Without it the discovered handler has no corpus and refuses
    every call. A per-persona ``ctx.extra['archivist.db_path']`` still overrides it.
    """
    db_env = os.environ.get("CABAL_ARCHIVIST_DB")
    config = ArchivistConfig(db_path=Path(db_env).expanduser()) if db_env else ArchivistConfig()
    return ArchivistHandler(default_config=config)


# ---- helpers ------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, (str, int, float))]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def _entry_to_dict(entry: Entry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "resource_name": entry.resource_name,
        "summary": entry.summary,
        "entry_type": entry.entry_type,
        "tags": entry.tags,
        "details": entry.details,
        "gotchas": entry.gotchas,
        "commands": entry.commands,
        "refs": entry.refs,
        "importance": entry.importance,
        "source_thread": entry.source_thread,
        "written_by": entry.written_by,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }


__all__ = ["ArchivistConfig", "ArchivistHandler", "get_handler"]
