"""Promotion bridge — distil an agent's techne knowledge into the Archivist.

Reads curated `knowledge` rows from an agent's techne SQLite (the
personal episodic → distilled layer) and publishes them into a *local*
Archivist as team-memory entries, with deterministic same-resource
supersession (`ArchivistStore.publish(..., supersede=True)`) so the store
converges instead of accumulating contradictions (agent-model design §5/§5e;
cabal Phase 1).

Store-direct + limenal-free: depends only on `cabal.archivist` and the
stdlib. techne knowledge is read via raw SQL to avoid coupling to techne
internals (the knowledge table schema is stable: id/type/content/tags/
confidence_numeric/source/created_at).

LIMITATION (Phase 1, documented): `resource_name` derivation is crude — a
``resource:<x>`` tag if present, else a kebab slug of the entry's first
heading/line. Stable resource naming is what makes supersession group
*re-distilled* lessons correctly; an LLM-assisted resource/summary
extraction is the real "distil" step and the documented follow-up. Until
then, tag distilled knowledge with ``resource:<stable-id>`` to get reliable
convergence.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from cabal.archivist.models import Entry, EntryType
from cabal.archivist.store import ArchivistStore

# techne knowledge.type  ->  Archivist entry_type
_TYPE_MAP: dict[str, EntryType] = {
    "fact": "fact",
    "decision": "decision",
    "procedure": "procedure",
    "pattern": "procedure",
    "evaluation": "fact",
}


@dataclass(slots=True)
class PromoteReport:
    """Tally of a promotion pass."""

    read: int = 0
    new: int = 0
    superseded: int = 0
    related: int = 0
    duplicate: int = 0
    skipped: int = 0
    entry_ids: list[str] = field(default_factory=list)


def _slugify(text: str, *, max_len: int = 60) -> str:
    """Kebab slug of the first non-empty line (heading markers stripped)."""
    first = ""
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            first = line
            break
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    return slug[:max_len] or "untitled"


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _resource_name(content: str, tags: list[str]) -> str:
    """`resource:<x>` tag wins (stable); else a slug of the first line."""
    for t in tags:
        if t.startswith("resource:") and t[len("resource:") :].strip():
            return t[len("resource:") :].strip()
    return _slugify(content)


def _summary(content: str, *, max_len: int = 280) -> str:
    """First non-empty line as the 'so what'; truncated."""
    for line in content.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:max_len]
    return content.strip()[:max_len] or "(empty)"


def _num(value: float | None, *, default: float) -> float:
    """Coerce a SQL numeric to float; None -> default. Deliberately NOT
    ``value or default``, which would corrupt a legitimate 0.0 into the
    default (Prudence review: resource:falsy-default-bug)."""
    return float(value) if value is not None else default


def promote_techne_knowledge(
    techne_db: Path,
    store: ArchivistStore,
    *,
    written_by: str,
    supersede: bool = True,
    min_confidence_numeric: float = 0.0,
    types: tuple[str, ...] | None = None,
) -> PromoteReport:
    """Promote an agent's distilled techne `knowledge` into `store`.

    Args:
        techne_db: path to the agent's techne SQLite.
        store: target (local) ArchivistStore.
        written_by: agent identity stamped on each entry (e.g. "Prudence").
        supersede: latest-wins per (resource_name, entry_type) — see
            ArchivistStore.publish. Default True (a converging store).
        min_confidence_numeric: skip knowledge below this confidence.
        types: restrict to these techne knowledge types (None = all mapped).
    """
    report = PromoteReport()
    conn = sqlite3.connect(str(techne_db))
    conn.row_factory = sqlite3.Row
    try:
        # Honour techne's status vocabulary. The destination is WRITE-ONLY —
        # export_corpus never deletes and ArchivistStore has no delete — so an
        # entry published here cannot be withdrawn by any code path. Selection
        # therefore FAILS CLOSED: an ALLOWLIST of `current`, not a denylist of
        # `archived`.
        #
        # A denylist publishes every status techne adds later by default. It
        # also already leaked one: `uncertain`, set by techne's doubt-cascade
        # (_cascade_uncertain) when an entry's supporting knowledge is
        # contradicted, and carrying UNCERTAIN_CONFIDENCE_PENALTY = 0.5. Those
        # were reaching the shared corpus at FULL un-penalised importance,
        # because the row below reads raw confidence_numeric rather than
        # techne's effective (decayed) confidence. An entry techne itself has
        # flagged as no-longer-trustworthy is exactly what should not be
        # published somewhere unretractable.
        #
        # Probed rather than assumed: this reads an ARBITRARY agent's techne db,
        # and `status` post-dates the original schema. A hard reference would
        # raise OperationalError on an older peer's store, turning a best-effort
        # sync into a hard failure — a worse outcome than promoting a superset.
        #
        # NULL is treated as `current`, deliberately, and it is NOT the same
        # decision as the allowlist above. A NULL means the row predates or
        # ignores the concept — the same situation as the absent column, which
        # also promotes. Without this, `status = 'current'` is NULL-propagating:
        # a store whose column was added without a default would promote NOTHING
        # AT ALL, silently, and a silent no-op is the failure mode this whole
        # change exists to remove. Fail closed on unknown VALUES; fail open on
        # an absent CONCEPT.
        has_status = any(
            r["name"] == "status" for r in conn.execute("PRAGMA table_info(knowledge)")
        )
        archived_clause = " AND (status IS NULL OR status = 'current')" if has_status else ""
        rows = conn.execute(
            f"""SELECT id, type, content, tags, source, confidence_numeric
               FROM knowledge
               WHERE superseded_by IS NULL{archived_clause}
               ORDER BY id"""
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        report.read += 1
        ttype = (row["type"] or "").strip()
        entry_type = _TYPE_MAP.get(ttype)
        if entry_type is None or (types is not None and ttype not in types):
            report.skipped += 1
            continue
        # Read confidence ONCE (None -> techne's 0.6 default) and use it for both
        # the filter and importance, so they can't disagree and a genuine 0.0
        # isn't corrupted to a default.
        conf = _num(row["confidence_numeric"], default=0.6)
        if conf < min_confidence_numeric:
            report.skipped += 1
            continue

        content = row["content"] or ""
        tags = _parse_tags(row["tags"])
        entry = Entry(
            resource_name=_resource_name(content, tags),
            summary=_summary(content),
            entry_type=entry_type,
            details=content,
            tags=[t for t in tags if not t.startswith("resource:")],
            importance=conf,
            source_thread=f"techne:knowledge:{row['id']}",
            written_by=written_by,
        )
        result = store.publish(entry, supersede=supersede)
        report.entry_ids.append(result.entry_id)
        if result.outcome == "new":
            report.new += 1
        elif result.outcome == "superseded":
            report.superseded += 1
        elif result.outcome == "related":
            report.related += 1
        else:  # duplicate_l0 / duplicate_l1
            report.duplicate += 1

    return report
