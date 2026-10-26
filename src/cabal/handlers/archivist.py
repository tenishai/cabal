"""archivist — Archivist's team-consensus knowledge interface.

Cabal-shipped liminal handler. Four tools:

  archivist.publish(resource_name, summary, entry_type, ...)
    Write an entry through the dedup pipeline. Returns the outcome,
    the entry id and related_ids:
      new           written; related_ids is empty
      duplicate_l0  same content as an active entry (L0 cache or L1
      duplicate_l1  hash), nothing written, entry_id is that entry;
                    related_ids is empty, or with supersede the other
                    entries in scope that it retired
      related      written and linked RELATED to up to 5 active
                    entries with the same resource_name and entry_type;
                    related_ids holds them
      superseded    written with supersede, retiring active entries in
                    scope; related_ids holds the retired ids

  archivist.search(query, entry_type?, tags?, top_k?)
    FTS5 search across the active entries of the team-memory corpus.
    The query is plain words, all of which must match. Filter by
    entry_type or tags (every tag must match).

  archivist.get(entry_id)
    Fetch one entry by id, with its outbound edges, its `status` (active
    or superseded) and `superseded_by`, the ids of the entries that
    replaced it.

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
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from liminal.protocol import Context, Handler, ToolResult, ToolSpec

from cabal.archivist import ArchivistStore, Entry
from cabal.archivist.store import SUPERSEDE_SCOPES
from cabal.core.observability.failures import log_failure

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
        {"name": "get", "description": "Fetch one entry by id (with edges, status and superseded_by)."},
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

    def status(self) -> str | None:
        """liminal#191: with no default corpus every call without a per-caller
        archivist.db_path is refused, so /healthz says so instead of ready."""
        if self.default_config.db_path is not None:
            return None
        return (
            "unconfigured: CABAL_ARCHIVIST_DB is unset, so only a per-caller "
            "archivist.db_path can name a corpus"
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="publish",
                description=(
                    "Publish an entry to team memory. The dedup pipeline "
                    "(L0 LRU → L1 hash → Tier-1 resource overlap) "
                    "decides whether to write or link. Returns outcome, "
                    "entry_id and related_ids. Outcomes: 'new' (written; "
                    "related_ids empty); 'duplicate_l0' / 'duplicate_l1' "
                    "(same content as an active entry, nothing written, "
                    "entry_id is that entry; related_ids empty, or with "
                    "supersede the other entries it retired); 'related' "
                    "(written and linked to up to 5 active entries with the "
                    "same resource_name and entry_type, listed in "
                    "related_ids); 'superseded' (written with supersede; "
                    "related_ids lists the entries it retired)."
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
                        "supersede": {
                            "type": "boolean",
                            "description": (
                                "When active entries share this resource_name "
                                "and entry_type, retire them instead of linking "
                                "them RELATED (the default). An identical "
                                "re-publish writes nothing; with supersede it "
                                "still retires the other active entries in "
                                "scope and lists them in related_ids."
                            ),
                        },
                        "supersede_scope": {
                            "type": "string",
                            "enum": list(SUPERSEDE_SCOPES),
                            "description": (
                                "Which entries a supersede retires. 'type' (the "
                                "default): same resource_name and entry_type. "
                                "'resource': every active entry with this "
                                "resource_name, whatever its type, for writers "
                                "that use one name per fact. No effect without "
                                "supersede; an unknown value is refused either way."
                            ),
                        },
                    },
                    "required": ["resource_name", "summary", "entry_type"],
                },
            ),
            ToolSpec(
                name="search",
                description=(
                    "Search the active entries of the team-memory corpus "
                    "(superseded entries are never returned). Results are "
                    "ranked by FTS5 bm25."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Plain words; every word must match. AND, OR "
                                "and NOT are ordinary words, and punctuation "
                                "is never an operator: it splits a word, so "
                                "'mash1.local' matches the phrase 'mash1 local'."
                            ),
                        },
                        "entry_type": {
                            "type": "string",
                            "enum": list(_VALID_ENTRY_TYPES),
                        },
                        "tags": {
                            "type": "array", "items": {"type": "string"},
                            "description": (
                                "Every tag must be on the entry (exact match). "
                                "A comma-separated string is also accepted."
                            ),
                        },
                        "top_k": {
                            "type": "integer", "minimum": 1, "maximum": 50,
                            "description": (
                                "Most results to return, default 10. A value "
                                "outside 1..50 is clamped to that range."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="get",
                description=(
                    "Fetch one entry by id, with its outbound edges. The entry's "
                    "`status` is `active` or `superseded`; a superseded entry is "
                    "retired, and `superseded_by` lists the ids that replaced it."
                ),
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
        started = time.monotonic()
        result = self._dispatch(name, payload, ctx)
        _log_call(name, payload, ctx, result, started)
        return result

    def _dispatch(
        self, name: str, payload: dict[str, Any], ctx: Context
    ) -> ToolResult:
        # Every path returns a ToolResult; nothing raises out of dispatch
        # (cabal#360).
        try:
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
        except _InvalidField as exc:
            return ToolResult.failure(str(exc))
        except sqlite3.Error as exc:
            # A database error is a failed call, not a crash (cabal#356).
            log.warning("archivist.db_error", tool=name, error=str(exc))
            return ToolResult.failure(f"archivist.{name}: database error: {exc}")
        except Exception as exc:
            # The message stays in the log: it can hold server paths, as an
            # OSError from init_db does. The caller gets the type only, as
            # liminal's LocalDispatcher gives it.
            log_failure(log, "archivist.unexpected_error", tool=name)
            return ToolResult.failure(f"archivist.{name}: internal error: {type(exc).__name__}")
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

        supersede = payload.get("supersede", False)
        if not isinstance(supersede, bool):
            return ToolResult.failure("'supersede' must be a boolean")

        supersede_scope = payload.get("supersede_scope", "type")
        if supersede_scope not in SUPERSEDE_SCOPES:
            return ToolResult.failure(
                f"'supersede_scope' must be one of {list(SUPERSEDE_SCOPES)}; "
                f"got {supersede_scope!r}"
            )

        entry = Entry(
            resource_name=payload["resource_name"].strip(),
            summary=payload["summary"].strip(),
            entry_type=entry_type,
            tags=_str_list(payload, "tags", allow_csv=True),
            details=_opt_str(payload, "details"),
            gotchas=_opt_str(payload, "gotchas"),
            commands=_str_list(payload, "commands"),
            refs=_str_list(payload, "refs"),
            importance=_importance(payload),
            source_thread=_opt_str(payload, "source_thread"),
            written_by=ctx.identity or "unknown",
        )

        result = store.publish(entry, supersede=supersede, supersede_scope=supersede_scope)
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
        tags = _str_list(payload, "tags", allow_csv=True) or None
        top_k = _top_k(payload, self.default_config.default_top_k)

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
        # A retired entry is still served by id, so say it is retired and
        # what replaced it (issue #357). Search already hides it.
        return ToolResult.json({
            "entry": _entry_to_dict(entry),
            "related": relations,
            "superseded_by": store.superseded_by(entry_id),
        })

    def _resolve_db_path(self, ctx: Context) -> Path | None:
        override = ctx.extra.get("archivist.db_path")
        if not override:
            return self.default_config.db_path
        if not isinstance(override, str):
            raise _InvalidField(
                "ctx.extra['archivist.db_path'] must be a string; "
                f"got {type(override).__name__}"
            )
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


class _InvalidField(ValueError):
    """A payload field has the wrong type or value. The message names the
    field and becomes the ToolResult failure (cabal#360)."""


def _opt_str(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None or isinstance(value, str):
        return value
    raise _InvalidField(f"'{name}' must be a string; got {type(value).__name__}")


def _str_list(payload: dict[str, Any], name: str, *, allow_csv: bool = False) -> list[str]:
    """A list of strings. With ``allow_csv`` (tags only) a comma-separated
    string is also accepted, as before; commands and refs are never split,
    since a command may hold a comma."""
    value = payload.get(name)
    if value is None:
        return []
    if allow_csv and isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    raise _InvalidField(f"'{name}' must be a list of strings")


def _importance(payload: dict[str, Any]) -> float:
    """None -> 1.0. NOT ``or 1.0``, which turns 0 into 1.0 (cabal#361)."""
    value = payload.get("importance")
    if value is None:
        return 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidField(
            f"'importance' must be a number from 0 to 1; got {type(value).__name__}"
        )
    if not 0.0 <= value <= 1.0:  # also refuses NaN
        raise _InvalidField(f"'importance' must be a number from 0 to 1; got {value!r}")
    return float(value)


def _top_k(payload: dict[str, Any], default: int) -> int:
    value = payload.get("top_k")
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidField(f"'top_k' must be an integer; got {type(value).__name__}")
    return max(1, min(value, 50))


def _log_call(
    name: str, payload: Any, ctx: Context, result: ToolResult, started: float
) -> None:
    """One debug line per call: tool, outcome, entry id, resource, written_by, error, duration."""
    outcome = "failure" if result.is_error else "ok"
    entry_id = resource = None
    data: Any = result.content[0].get("data") if result.content else None
    if isinstance(data, dict):
        outcome = data.get("outcome", outcome)
        entry_id = data.get("entry_id")
        if isinstance(data.get("entry"), dict):
            entry_id = data["entry"].get("id")
            resource = data["entry"].get("resource_name")
    if isinstance(payload, dict):
        entry_id = entry_id or payload.get("entry_id")
        resource = resource or payload.get("resource_name")
    log.debug(
        "archivist.call",
        tool=name,
        outcome=outcome,
        entry_id=entry_id,
        resource=resource,
        written_by=ctx.identity,
        error=result.error,
        duration_ms=round((time.monotonic() - started) * 1000, 1),
    )


def _entry_to_dict(entry: Entry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "resource_name": entry.resource_name,
        "summary": entry.summary,
        "entry_type": entry.entry_type,
        "status": entry.status,
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
