"""Filesystem adapter — walks a directory tree, yields one Document per file.

v1 supports plain-text formats (.md / .txt). PDF (.pdf) and Word
(.docx) extraction are follow-ups; they require optional deps
(pypdf, python-docx) that aren't worth pulling into the cabal core.
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cabal.librarian.adapter import Document, DocumentRef, SourceHealth

__all__ = ["FilesystemAdapter"]


# Filename → kind tagging. The first matching pattern wins.
KIND_HINTS = (
    ("readme*", "readme"),
    ("runbook*", "runbook"),
    ("adr-*", "adr"),
    ("*-adr.md", "adr"),
    ("decision-*", "adr"),
    ("api-*", "api-spec"),
    ("openapi*", "api-spec"),
)


# Conservative defaults — anything larger than this is probably a
# generated artifact, not docs.
DEFAULT_MAX_BYTES = 1_000_000  # 1 MB


@dataclass
class FilesystemAdapter:
    """Walk a local directory and emit Documents for matching files.

    Args:
        name: Logical name of the source (used in URIs and tags).
        roots: One or more base directories to walk.
        include_globs: fnmatch patterns; at least one must match (basename
            OR full POSIX path) for a file to be ingested.
        exclude_globs: Patterns whose match excludes the file.
        max_bytes: Files larger than this are skipped (with no record).
    """

    name: str
    roots: list[Path]
    include_globs: list[str] = field(default_factory=lambda: ["*.md", "*.txt"])
    exclude_globs: list[str] = field(
        default_factory=lambda: ["**/.git/**", "**/node_modules/**"]
    )
    max_bytes: int = DEFAULT_MAX_BYTES

    async def authenticate(self, credentials: dict[str, str]) -> None:
        """Filesystem adapter takes no credentials."""
        return None

    async def list_documents(self) -> AsyncIterator[DocumentRef]:
        for root in self.roots:
            if not root.exists():
                continue
            if root.is_file():
                ref = self._maybe_ref(root)
                if ref is not None:
                    yield ref
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                ref = self._maybe_ref(path)
                if ref is not None:
                    yield ref

    async def fetch_document(self, ref: DocumentRef) -> Document:
        path = self._resolve_uri(ref.uri)
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = _kind_for(path)
        tags = [f"source:{self.name}", f"kind:{kind}"]
        metadata = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "mtime": ref.last_modified.isoformat(),
        }
        return Document(ref=ref, content=text, kind=kind, tags=tags, metadata=metadata)

    async def healthcheck(self) -> SourceHealth:
        for root in self.roots:
            if not root.exists():
                return SourceHealth(
                    healthy=False, detail=f"root {root} does not exist"
                )
        return SourceHealth(healthy=True)

    # ---- internals ----------------------------------------------------

    def _maybe_ref(self, path: Path) -> DocumentRef | None:
        """Return a DocumentRef iff path passes glob + size checks."""
        if not _matches(path, self.include_globs, self.exclude_globs):
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > self.max_bytes:
            return None
        rel = self._uri_relative(path)
        return DocumentRef(
            uri=f"librarian://{self.name}/{rel}",
            title=path.name,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            content_hash=_hash_path(path),
        )

    def _uri_relative(self, path: Path) -> str:
        """Path expressed relative to the first matching root, else absolute."""
        for root in self.roots:
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                continue
        return path.as_posix().lstrip("/")

    def _resolve_uri(self, uri: str) -> Path:
        """Walk roots looking for the relative path this URI came from."""
        prefix = f"librarian://{self.name}/"
        if not uri.startswith(prefix):
            raise ValueError(f"uri {uri!r} does not match adapter {self.name!r}")
        rel = uri[len(prefix):]
        for root in self.roots:
            candidate = root / rel
            if candidate.is_file():
                return candidate
        # Fallback for the absolute-path case
        absolute = Path("/" + rel.lstrip("/"))
        if absolute.is_file():
            return absolute
        raise FileNotFoundError(f"cannot resolve {uri}")


# ---- module-level helpers --------------------------------------------------


def _matches(path: Path, includes: list[str], excludes: list[str]) -> bool:
    name = path.name
    posix = path.as_posix()
    if not any(
        fnmatch.fnmatch(name, g) or fnmatch.fnmatch(posix, g) for g in includes
    ):
        return False
    if any(
        fnmatch.fnmatch(name, g) or fnmatch.fnmatch(posix, g) for g in excludes
    ):
        return False
    return True


def _kind_for(path: Path) -> str:
    name = path.name.lower()
    for pattern, kind in KIND_HINTS:
        if fnmatch.fnmatch(name, pattern):
            return kind
    return "doc"


def _hash_path(path: Path) -> str:
    """Cheap content_hash — size + mtime, NOT a content digest. Sufficient
    for change-detection during periodic re-walks; a real content digest
    would be a follow-up if we want exact dedup."""
    stat = path.stat()
    raw = f"{stat.st_size}:{stat.st_mtime}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


