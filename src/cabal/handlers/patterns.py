"""patterns — Architect's pattern-corpus read interface.

Cabal-shipped limenal handler. Exposes the techne knowledge corpus
filtered to `type="pattern"` as MCP tools any agent (the Architect
persona, Claude Code via MCP, third-party clients) can call.

Tools:
  patterns.search(query, top_k?, half_life_days?)
    Hybrid recall over the patterns corpus. Returns top_k entries
    with content, source URL, confidence (decay-adjusted per
    techne#13), surveyed date, and the relevance score.
  patterns.record(name, source_url, what, why, where_to_apply, tags?)
    Append a new pattern. Operator-only via scope check; the
    pattern body is rendered using the same H1-then-prose shape
    `techne import-patterns` produces, so external surveys and
    record-via-MCP write identical schemas.

Lazy-imports techne — handler module loads without techne installed;
dispatch fails with a structured "techne not installed" message when
the user calls a tool. Same shape as limenal/handlers/memory.py.

Configured per-persona via `ctx.extra["patterns.db_path"]`. Defaults
to `default_config.db_path` (operator-set) when no override arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import anyio
import structlog
from limenal.protocol import Context, Handler, ToolResult, ToolSpec

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
        override arrives. Should point at the techne DB holding the
        pattern corpus.
    use_vectors: whether to enable vector recall alongside FTS5 (techne
        applies RRF fusion automatically when both channels return).
    default_top_k: default search limit when caller doesn't specify.
    default_half_life_days: decay half-life applied to confidence
        scores in search results. 0 disables decay (patterns rarely go
        stale; default 0).
    """

    db_path: Path | None = None
    use_vectors: bool = True
    default_top_k: int = 10
    default_half_life_days: float = 0.0


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
                        "half_life_days": {
                            "type": "number",
                            "description": (
                                "Override the confidence-decay half-life. "
                                "0 disables decay. Defaults to the handler's "
                                "default_half_life_days."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="record",
                description=(
                    "Record a new architectural pattern. Operator scope required "
                    "(checked by the limenal route layer)."
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
                        "tags": {
                            "type": "string",
                            "description": (
                                "Comma-separated tags; appended to "
                                "surveyed:<today>."
                            ),
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
            import techne  # type: ignore[import-not-found]
        except ImportError as exc:
            return ToolResult.failure(
                f"techne not installed; patterns handler unavailable ({exc}). "
                "Install via `pip install cabal[memory]` or pip-install techne "
                "directly."
            )

        if name == "search":
            return await self._search(techne, db_path, payload)
        if name == "record":
            return await self._record(techne, db_path, payload)
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

    async def _search(self, techne: Any, db_path: Path, payload: dict[str, Any]) -> ToolResult:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.failure("'query' must be a non-empty string")

        top_k = int(payload.get("top_k") or self.default_config.default_top_k)
        top_k = max(1, min(top_k, 50))

        half_life = float(
            payload.get("half_life_days", self.default_config.default_half_life_days)
        )

        try:
            results = await anyio.to_thread.run_sync(
                lambda: techne.search.search(
                    db_path,
                    query,
                    table="knowledge",
                    limit=top_k,
                    use_vectors=self.default_config.use_vectors,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"patterns search failed: {exc}")

        # Filter to type="pattern" only (knowledge.search returns the broader
        # corpus) and attach effective_confidence per entry.
        hits = []
        for row in results:
            if row.get("knowledge_type") != "pattern" and row.get("type") != "pattern":
                # Some result rows expose `knowledge_type`, others `type`; both
                # are checked. Pattern rows pass through; others (no-lift
                # entries are also type="pattern" with a `surveyed-no-lift` tag)
                # are filtered out unless they're patterns.
                continue
            tags = row.get("tags") or ""
            # Skip no-lift entries — they're for dedup, not for recall.
            if "surveyed-no-lift" in tags:
                continue

            entry_id = row.get("id")
            effective = None
            if entry_id is not None:
                try:
                    # default-arg trick binds the loop variable into the lambda
                    effective = await anyio.to_thread.run_sync(
                        lambda eid=entry_id: techne.knowledge.effective_confidence(
                            db_path, eid, half_life_days=half_life,
                        )
                    )
                except Exception:  # noqa: BLE001
                    effective = None

            hits.append({
                "id": entry_id,
                "content": row.get("content", ""),
                "source_url": row.get("source"),
                "tags": tags,
                "confidence": row.get("confidence"),
                "effective_confidence": effective,
                "score": row.get("rrf_score") or row.get("rank"),
                "match_source": row.get("match_source"),
            })

        return ToolResult.json({"query": query, "hits": hits[:top_k]})

    async def _record(
        self, techne: Any, db_path: Path, payload: dict[str, Any]
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
        extra_tags = payload.get("tags") or ""

        # Render body in the same H1-then-prose shape `techne import-patterns`
        # writes, so corpus entries are interchangeable regardless of which
        # ingest path created them.
        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        content = (
            f"# {name}\n\n"
            f"{body_what}\n\n"
            f"**Why interesting**: {body_why}\n\n"
            f"**Where to apply**: {body_where}\n"
        )
        tags = f"surveyed:{today}"
        if extra_tags.strip():
            tags = f"{extra_tags.strip()},{tags}"

        try:
            entry_id = await anyio.to_thread.run_sync(
                lambda: techne.knowledge.store(
                    db_path,
                    content=content,
                    type="pattern",
                    tags=tags,
                    confidence="medium",
                    source=source_url,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"patterns record failed: {exc}")

        return ToolResult.json(
            {"entry_id": entry_id, "name": name, "source_url": source_url}
        )


def get_handler() -> Handler:
    """Entry-point factory for the patterns handler."""
    return PatternsHandler()
