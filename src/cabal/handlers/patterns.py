"""patterns — Architect's pattern-corpus read interface.

Cabal-shipped liminal handler. Exposes the techne Architect pattern
store (`architect.db`, techne#87/#137) as MCP tools any agent (the
Architect persona, Claude Code via MCP, third-party clients) can call.

Tools:
  patterns.search(query, top_k?)
    FTS recall over the active patterns in architect.db. Returns
    top_k entries with name, body, source URL, confidence, and the
    BM25 relevance score.
  patterns.record(name, source_url, what, why, where_to_apply)
    Append a new pattern to architect.db — an investigation row plus
    a pattern row, idempotent by content hash. Operator-only via
    scope check.

Lazy-imports techne — handler module loads without techne installed;
dispatch fails with a structured "techne not installed" message when
the user calls a tool. Same shape as liminal/handlers/memory.py.

Configured per-persona via `ctx.extra["patterns.db_path"]` (point it at
architect.db). Falls back to `default_config.db_path` (operator-set)
when no override arrives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import anyio
import structlog
from liminal.protocol import Context, Handler, ToolResult, ToolSpec

log = structlog.get_logger(__name__)


CARD = {
    "handler": "patterns",
    "description": (
        "Architect's pattern-corpus read interface. Search and record "
        "architectural patterns lifted from external projects or extracted "
        "from internal practice."
    ),
    "tools": [
        {
            "name": "search",
            "description": "Find patterns matching a query.",
        },
        {
            "name": "record",
            "description": "Append a new pattern to the corpus (operator-only).",
        },
    ],
    "activation": "always",
    "relevance_signals": [
        "looking up prior art on how to build something",
        "checking how the team has solved this kind of problem before",
        "finding existing tools or libraries for a recurring need",
        "deciding between architectural approaches",
    ],
    "credential_keys": [],
    "labels": {"tier": "cabal-shipped", "backend": "techne", "persona": "architect"},
}


@dataclass
class PatternsConfig:
    """Operator config for the patterns handler.

    db_path: fallback when no per-persona ctx.extra["patterns.db_path"]
        override arrives. Should point at the Architect store
        (`architect.db`) holding the pattern corpus.
    default_top_k: default search limit when caller doesn't specify.
    """

    db_path: Path | None = None
    default_top_k: int = 10


@dataclass
class PatternsHandler:
    default_config: PatternsConfig = field(default_factory=PatternsConfig)

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search",
                description=(
                    "Search architectural patterns by query. Returns matching "
                    "entries with content, source URL, confidence, and score."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {
                            "type": "integer", "minimum": 1, "maximum": 50,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="record",
                description=(
                    "Record a new architectural pattern. Operator scope required "
                    "(checked by the liminal route layer)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short pattern name (H1 heading).",
                        },
                        "source_url": {
                            "type": "string",
                            "description": "Where the pattern came from.",
                        },
                        "what": {
                            "type": "string",
                            "description": "What the pattern does (2-4 sentences).",
                        },
                        "why": {
                            "type": "string",
                            "description": "Why it's interesting (one sentence).",
                        },
                        "where_to_apply": {
                            "type": "string",
                            "description": "Where it fits in our codebase.",
                        },
                    },
                    "required": ["name", "source_url", "what", "why", "where_to_apply"],
                },
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
                "no patterns corpus configured: set default_config.db_path or "
                "pass ctx.extra['patterns.db_path']"
            )

        try:
            import techne  # type: ignore[import-not-found]  # noqa: F401
            from techne.core import patterns as tp  # type: ignore[import-not-found]
        except ImportError as exc:
            return ToolResult.failure(
                f"techne not installed; patterns handler unavailable ({exc}). "
                "Install via `pip install cabal[memory]` or pip-install techne "
                "directly."
            )

        if name == "search":
            return await self._search(tp, db_path, payload)
        if name == "record":
            return await self._record(tp, db_path, payload)
        return ToolResult.failure(f"unknown tool {name!r}")

    # ---- config resolution ----------------------------------------------

    def _resolve_db_path(self, ctx: Context) -> Path | None:
        override = ctx.extra.get("patterns.db_path")
        if not override:
            return self.default_config.db_path
        candidate = Path(override).expanduser()
        # Reject `..` traversal — matches the memory handler's posture.
        if any(part == ".." for part in candidate.parts):
            log.warning("patterns.db_path_rejected", reason="traversal", path=override)
            return None
        return candidate

    # ---- tools ----------------------------------------------------------

    async def _search(self, tp: Any, db_path: Path, payload: dict[str, Any]) -> ToolResult:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.failure("'query' must be a non-empty string")

        top_k = int(payload.get("top_k") or self.default_config.default_top_k)
        top_k = max(1, min(top_k, 50))

        try:
            # search_patterns is FTS over ACTIVE patterns only; no-lift entries
            # live in the investigations table (verdict=skip*), so they never
            # surface here — no client-side filtering needed.
            results = await anyio.to_thread.run_sync(
                lambda: tp.search_patterns(db_path, query, limit=top_k)
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"patterns search failed: {exc}")

        hits = [
            {
                "id": row.get("id"),
                "name": row.get("name", ""),
                "content": row.get("body", ""),
                "source_url": row.get("source_url"),
                "confidence": row.get("confidence"),
                "score": row.get("rank"),
            }
            for row in results
        ]

        return ToolResult.json({"query": query, "hits": hits[:top_k]})

    async def _record(
        self, tp: Any, db_path: Path, payload: dict[str, Any]
    ) -> ToolResult:
        required = ("name", "source_url", "what", "why", "where_to_apply")
        for field_name in required:
            val = payload.get(field_name)
            if not isinstance(val, str) or not val.strip():
                return ToolResult.failure(
                    f"{field_name!r} must be a non-empty string"
                )

        name = payload["name"].strip()
        source_url = payload["source_url"].strip()
        body_what = payload["what"].strip()
        body_why = payload["why"].strip()
        body_where = payload["where_to_apply"].strip()

        # The Architect store keeps `name` as its own column; the body holds the
        # what/why/where prose (same shape `architect ingest` parses out of a
        # patterns-md section, minus the H2 heading which becomes `name`).
        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        body = (
            f"{body_what}\n\n"
            f"**Why interesting**: {body_why}\n\n"
            f"**Where to apply**: {body_where}\n"
        )

        def _ingest() -> tuple[str, bool]:
            inv_id = tp.upsert_investigation(
                db_path, url=source_url, surveyed_at=today, verdict="full-eval"
            )
            return tp.add_pattern(
                db_path,
                investigation_id=inv_id,
                name=name,
                source_url=source_url,
                body=body,
                confidence="medium",
            )

        try:
            pattern_id, stored = await anyio.to_thread.run_sync(_ingest)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"patterns record failed: {exc}")

        return ToolResult.json(
            {
                "pattern_id": pattern_id,
                "stored": stored,
                "name": name,
                "source_url": source_url,
            }
        )


def get_handler() -> Handler:
    """Entry-point factory for the patterns handler.

    Reads ``CABAL_ARCHITECT_DB`` from the environment (if set) to wire the
    served handler's fallback corpus path, so ``liminal serve`` can point at
    a fixed ``architect.db`` without every caller supplying it per-call. A
    per-persona ``ctx.extra['patterns.db_path']`` still overrides it.
    """
    db_env = os.environ.get("CABAL_ARCHITECT_DB")
    config = (
        PatternsConfig(db_path=Path(db_env).expanduser()) if db_env else PatternsConfig()
    )
    return PatternsHandler(default_config=config)
