"""RepoIndexer — walk a code repo and ingest each source file as a
techne knowledge entry.

Reuses the Librarian's FilesystemAdapter for the walking + globbing
logic; the difference is the tagging (kind:code, language:<ext>) and
that each file is stored verbatim (no kind classification by filename
pattern).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from cabal.librarian.adapters.filesystem import FilesystemAdapter

logger = logging.getLogger(__name__)

DEFAULT_INCLUDE_GLOBS = [
    "*.py", "*.js", "*.ts", "*.tsx", "*.jsx", "*.go", "*.rs",
    "*.java", "*.kt", "*.swift", "*.rb", "*.php", "*.cs",
    "*.c", "*.h", "*.cpp", "*.hpp", "*.cc",
    "*.sh", "*.bash", "*.zsh", "*.fish",
    "*.sql", "*.proto", "*.toml", "*.yaml", "*.yml",
]

DEFAULT_EXCLUDE_GLOBS = [
    "**/.git/**", "**/node_modules/**", "**/.venv/**", "**/venv/**",
    "**/__pycache__/**", "**/dist/**", "**/build/**", "**/.next/**",
    "**/target/**", "**/.pytest_cache/**",
]


# Extension → language tag. Anything not in this map gets language:other.
EXT_TO_LANGUAGE = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".swift": "swift", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".fish": "shell",
    ".sql": "sql", ".proto": "protobuf",
    ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
}


@dataclass(slots=True)
class CodeRepo:
    """One indexed code repository."""

    name: str
    root: Path
    include_globs: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_GLOBS))
    exclude_globs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))
    max_bytes: int = 1_000_000  # 1 MB per file


@dataclass(slots=True)
class IndexResult:
    repos_indexed: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.repos_indexed.values())


class RepoIndexer:
    """Indexes one or more CodeRepos into a techne knowledge corpus.

    Storage is the same `type="document"` knowledge entry shape the
    Librarian uses — different tags identify code from docs.
    """

    def __init__(self, db_path: Path, repos: list[CodeRepo]) -> None:
        self.db_path = db_path
        self.repos = {r.name: r for r in repos}

    async def refresh(self, repo_name: str | None = None) -> IndexResult:
        result = IndexResult()
        targets = (
            [self.repos[repo_name]]
            if repo_name is not None
            else list(self.repos.values())
        )
        existing = self._existing_uris()
        for repo in targets:
            count = await self._index_one(repo, existing)
            result.repos_indexed[repo.name] = count
        return result

    async def _index_one(self, repo: CodeRepo, existing: set[str]) -> int:
        adapter = FilesystemAdapter(
            name=repo.name,
            roots=[repo.root],
            include_globs=repo.include_globs,
            exclude_globs=repo.exclude_globs,
            max_bytes=repo.max_bytes,
        )
        count = 0
        async for ref in adapter.list_documents():
            if ref.uri in existing:
                continue
            try:
                doc = await adapter.fetch_document(ref)
            except Exception:  # noqa: BLE001
                logger.exception("codex.fetch_failed uri=%s", ref.uri)
                continue
            language = _language_for(Path(ref.title))
            tags = [
                f"source:{repo.name}",
                "kind:code",
                f"language:{language}",
            ]
            self._store(doc.content, doc.ref.title, doc.ref.uri, tags)
            existing.add(ref.uri)
            count += 1
        return count

    def _store(self, content: str, title: str, uri: str, tags: list[str]) -> None:
        import techne

        tagged_content = f"# {title}\n\n{content}".rstrip() + "\n"
        techne.knowledge.store(
            self.db_path,
            content=tagged_content,
            type="document",
            tags=",".join(tags),
            confidence="medium",
            source=uri,
        )

    def _existing_uris(self) -> set[str]:
        from techne.core.database import connect

        if not self.db_path.exists():
            return set()
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT source FROM knowledge
                   WHERE type = 'document' AND superseded_by IS NULL
                     AND source IS NOT NULL
                     AND tags LIKE '%kind:code%'""",
            ).fetchall()
        finally:
            conn.close()
        return {row["source"] for row in rows}


def _language_for(path: Path) -> str:
    return EXT_TO_LANGUAGE.get(path.suffix.lower(), "other")
