"""Archivist data model — Entry + Relation dataclasses.

Mirrors the schema from docs/agent-architecture.md §Archivist. Field
notes:
  - `resource_name`: kebab-case identifier (the "what"). Required.
  - `summary`: main content (the "so what"). Required.
  - `entry_type`: drives query-shape selection downstream; the search
    layer can weight retrieval by type ("why did we choose X?" weights
    `decision` entries; "have we seen this error?" weights `error`).
  - `gotchas`: rare (~1% fill rate) but disproportionately valuable;
    elevated to a dedicated field so it can't get lost in body text.
  - `importance`: 0..1 base weight; the read pipeline can apply
    techne#13-style decay against `updated_at` if recency matters.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

EntryType = Literal[
    "fact",
    "decision",
    "operation",
    "error",
    "procedure",
    "document",
]


RelationType = Literal[
    "SUPERSEDES",
    "CONTRADICTS",
    "SUPPLEMENTS",
    "RELATED",
]


@dataclass(slots=True)
class Entry:
    """One Archivist knowledge entry."""

    resource_name: str
    summary: str
    entry_type: EntryType
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tags: list[str] = field(default_factory=list)
    details: str | None = None
    gotchas: str | None = None
    commands: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    importance: float = 1.0
    source_thread: str | None = None
    content_hash: str = ""  # set by Entry.compute_hash() if empty
    status: str = "active"  # active | superseded
    processing_state: str = "indexed"  # pending | indexed
    written_by: str = "unknown"
    access_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def compute_hash(self) -> str:
        """Stable SHA-256 over the full *content* — every field a legitimate
        update would change — so a re-distilled entry whose summary is unchanged
        but whose body evolved is NOT mistaken for a duplicate, and can reach the
        supersede path (#26). Excludes provenance/metadata (id, tags, importance,
        written_by, timestamps): those aren't content. Control-char separators
        avoid field-boundary collisions."""
        import hashlib

        material = "\x01".join(
            [
                self.resource_name,
                self.entry_type,
                self.summary,
                self.details or "",
                self.gotchas or "",
                "\x02".join(self.commands),
                "\x02".join(self.refs),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def to_row(self) -> dict[str, Any]:
        """Adapt to INSERT parameter dict."""
        if not self.content_hash:
            self.content_hash = self.compute_hash()
        now = datetime.now(UTC).isoformat()
        if self.created_at is None:
            self.created_at = now
        self.updated_at = now
        return {
            "id": self.id,
            "resource_name": self.resource_name,
            "summary": self.summary,
            "entry_type": self.entry_type,
            "tags": json.dumps(self.tags),
            "details": self.details,
            "gotchas": self.gotchas,
            "commands": json.dumps(self.commands),
            "refs": json.dumps(self.refs),
            "importance": self.importance,
            "source_thread": self.source_thread,
            "content_hash": self.content_hash,
            "status": self.status,
            "processing_state": self.processing_state,
            "written_by": self.written_by,
            "access_count": self.access_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Entry:
        """Adapt from a SELECT row dict."""
        return cls(
            id=row["id"],
            resource_name=row["resource_name"],
            summary=row["summary"],
            entry_type=row["entry_type"],
            tags=_loads_or_default(row.get("tags"), []),
            details=row.get("details"),
            gotchas=row.get("gotchas"),
            commands=_loads_or_default(row.get("commands"), []),
            refs=_loads_or_default(row.get("refs"), []),
            # None -> default; NOT `or 1.0`, which would corrupt a stored 0.0.
            importance=(float(row["importance"]) if row.get("importance") is not None else 1.0),
            source_thread=row.get("source_thread"),
            content_hash=row.get("content_hash") or "",
            status=row.get("status") or "active",
            processing_state=row.get("processing_state") or "indexed",
            written_by=row.get("written_by") or "unknown",
            access_count=int(row.get("access_count") or 0),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass(slots=True)
class Relation:
    """A directed edge between two entries."""

    src_id: str
    dst_id: str
    relation_type: RelationType
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None


def _loads_or_default(value: Any, default: Any) -> Any:
    """Tolerant JSON-or-default decoder for legacy / missing data."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
