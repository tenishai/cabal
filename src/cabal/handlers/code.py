"""code — Codex's code-corpus interface.

Cabal-shipped liminal handler. Three tools:

  code.search(query, language?, repo?, top_k?)
    Hybrid recall over indexed code files (techne knowledge entries
    tagged `kind:code`). Filters by language tag (python / typescript /
    go / ...) and by repo (source tag).

  code.who_wrote(file_path, line?)
    git blame on a file. Returns one BlameLine when `line` is given,
    or an authorship-summary {authors: [...], last_modified: ...}
    when not.

  code.list_repos()
    Configured repos + their root paths.

Configured at construction with a CodexConfig holding a list of
CodeRepo objects. Operators set the corpus DB path and the repos to
index.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog
from liminal.protocol import Context, Handler, ToolResult, ToolSpec

from cabal.codex import BlameError, CodeRepo, RepoIndexer, blame_line, blame_summary

log = structlog.get_logger(__name__)


CARD = {
    "handler": "code",
    "description": (
        "Codex's code-corpus interface — search code, find authorship, "
        "list indexed repos."
    ),
    "tools": [
        {"name": "search", "description": "Search the code corpus by query."},
        {"name": "who_wrote", "description": "git blame for a file (or one line)."},
        {"name": "list_repos", "description": "What code repos are indexed."},
        {
            "name": "refresh",
            "description": "Re-index one or all repos (operator-only).",
            "requires_operator": True,
        },
    ],
    "activation": "always",
    "relevance_signals": [
        "finding code that does X",
        "looking up who wrote a function or line",
        "discovering what files exist in a repo",
        "checking the SME for a piece of code",
    ],
    "credential_keys": [],
    "labels": {"tier": "cabal-shipped", "backend": "techne", "persona": "codex"},
}


@dataclass
class CodexConfig:
    db_path: Path | None = None
    repos: list[CodeRepo] = field(default_factory=list)
    default_top_k: int = 10


@dataclass
class CodeHandler:
    default_config: CodexConfig = field(default_factory=CodexConfig)

    def status(self) -> str | None:
        """liminal#191: with no default corpus every call without a per-caller
        code.db_path is refused, so /healthz says so instead of ready."""
        if self.default_config.db_path is not None:
            return None
        return (
            "unconfigured: no codex db (get_handler reads no environment "
            "variable), so only a per-caller code.db_path can name a corpus"
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search",
                description="Search the code corpus by query.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "language": {
                            "type": "string",
                            "description": "Filter to one language (python, go, ts, ...)",
                        },
                        "repo": {
                            "type": "string",
                            "description": "Filter to one repo by configured name.",
                        },
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="who_wrote",
                description="git blame for a file (or one line if `line` given).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path relative to repo root (or absolute).",
                        },
                        "repo": {
                            "type": "string",
                            "description": "Repo name from list_repos (sets git cwd).",
                        },
                        "line": {
                            "type": "integer",
                            "description": "Optional line number. Omit for whole-file summary.",
                        },
                    },
                    "required": ["file_path"],
                },
            ),
            ToolSpec(
                name="list_repos",
                description="List configured repos.",
                input_schema={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="refresh",
                description="Re-index one or all repos.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                    },
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
                "no codex corpus configured: set default_config.db_path "
                "or pass ctx.extra['code.db_path']"
            )
        try:
            import techne  # noqa: F401
        except ImportError as exc:
            return ToolResult.failure(
                f"techne not installed; code handler unavailable ({exc})"
            )

        if name == "search":
            return await self._search(db_path, payload)
        if name == "who_wrote":
            return await self._who_wrote(payload)
        if name == "list_repos":
            return self._list_repos()
        if name == "refresh":
            return await self._refresh(db_path, payload)
        return ToolResult.failure(f"unknown tool {name!r}")

    # ---- tools ---------------------------------------------------------

    async def _search(self, db_path: Path, payload: dict[str, Any]) -> ToolResult:
        import techne
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.failure("'query' must be a non-empty string")
        language = payload.get("language") or None
        repo = payload.get("repo") or None
        top_k = int(payload.get("top_k") or self.default_config.default_top_k)
        top_k = max(1, min(top_k, 50))

        results = techne.search.search(
            db_path, query, table="knowledge",
            limit=max(top_k * 4, top_k), use_vectors=True,
        )
        hits: list[dict[str, Any]] = []
        for row in results:
            if row.get("knowledge_type") != "document" and row.get("type") != "document":
                continue
            tags = row.get("tags") or ""
            if "kind:code" not in tags:
                continue
            if language is not None and f"language:{language}" not in tags:
                continue
            if repo is not None and f"source:{repo}" not in tags:
                continue
            hits.append({
                "uri": row.get("source"),
                "content": row.get("content", "")[:2000],
                "tags": tags,
                "score": row.get("rrf_score") or row.get("rank"),
                "match_source": row.get("match_source"),
            })
            if len(hits) >= top_k:
                break
        return ToolResult.json({"query": query, "hits": hits})

    async def _who_wrote(self, payload: dict[str, Any]) -> ToolResult:
        path_str = payload.get("file_path")
        if not isinstance(path_str, str) or not path_str.strip():
            return ToolResult.failure("'file_path' must be a non-empty string")
        repo_name = payload.get("repo") or None
        line = payload.get("line")

        path = Path(path_str)
        repo_root: Path | None = None
        if repo_name is not None:
            repo = next(
                (r for r in self.default_config.repos if r.name == repo_name), None,
            )
            if repo is None:
                return ToolResult.failure(
                    f"unknown repo {repo_name!r}; configured: "
                    f"{[r.name for r in self.default_config.repos]}"
                )
            repo_root = repo.root
            if not path.is_absolute():
                path = repo.root / path
        try:
            if line is not None:
                result = await blame_line(path, int(line), repo_root=repo_root)
                return ToolResult.json({"line": asdict(result)})
            summary = await blame_summary(path, repo_root=repo_root)
            return ToolResult.json({"summary": summary})
        except BlameError as exc:
            return ToolResult.failure(str(exc))

    def _list_repos(self) -> ToolResult:
        repos = [
            {"name": r.name, "root": str(r.root)}
            for r in self.default_config.repos
        ]
        return ToolResult.json({"repos": repos})

    async def _refresh(self, db_path: Path, payload: dict[str, Any]) -> ToolResult:
        # Operator scope is enforced by liminal's gate on the card's requires_operator flag (#246).
        repo_name = payload.get("repo") or None
        if repo_name is not None:
            if not any(r.name == repo_name for r in self.default_config.repos):
                return ToolResult.failure(f"unknown repo {repo_name!r}")
        indexer = RepoIndexer(db_path=db_path, repos=self.default_config.repos)
        result = await indexer.refresh(repo_name)
        return ToolResult.json({
            "indexed": result.repos_indexed,
            "total": result.total,
        })

    def _resolve_db_path(self, ctx: Context) -> Path | None:
        override = ctx.extra.get("code.db_path")
        if not override:
            return self.default_config.db_path
        candidate = Path(override).expanduser()
        if any(part == ".." for part in candidate.parts):
            log.warning("code.db_path_rejected", reason="traversal", path=override)
            return None
        return candidate


def get_handler() -> Handler:
    return CodeHandler()


# Silence unused-import warning for json (re-exported intent)
_ = json


__all__ = ["CodeHandler", "CodexConfig", "get_handler"]
