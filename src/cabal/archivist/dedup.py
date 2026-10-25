"""Dedup pipeline — L0 LRU + L1 DB hash + Tier 1 overlap detection.

Tier 2 (LLM SUPERSEDES/CONTRADICTS classification) is intentionally
omitted from this MVP; it requires the LLMClient + an async worker
pattern. Today every entry that survives L0+L1 reaches Tier 1, gets
RELATED edges if overlapping entries exist, and is marked
processing_state='indexed'.

The 4-layer ladder is the documented architecture (see
docs/agent-architecture.md §Archivist); shipping three layers now and
the fourth as a follow-up preserves the public dispatch surface.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cabal.archivist.models import Entry, Relation
from cabal.archivist.schema import connect

DedupOutcome = Literal["new", "duplicate_l0", "duplicate_l1", "related"]


@dataclass(slots=True)
class DedupResult:
    """What the dedup pipeline decided about an incoming entry."""

    outcome: DedupOutcome
    entry_id: str | None = None  # The stored entry's id (new or matched dup)
    related_ids: list[str] = field(default_factory=list)  # Tier 1 overlaps


class Dedupper:
    """In-memory LRU + DB-backed dedup checks for Archivist writes."""

    def __init__(self, db_path: Path, *, l0_capacity: int = 1024) -> None:
        self.db_path = db_path
        self._l0_capacity = l0_capacity
        self._l0_cache: OrderedDict[str, str] = OrderedDict()

    def check(self, entry: Entry) -> DedupResult:
        """Run L0 → L1 → Tier-1 against `entry`. Does NOT write.

        Returns a DedupResult; the caller decides whether to commit.
        """
        if not entry.content_hash:
            entry.content_hash = entry.compute_hash()

        conn = connect(self.db_path)
        try:
            # L0: in-memory LRU cache of content_hash → entry_id. A hit counts only
            # while that entry is still active (#353): a supersede retires entries
            # without touching this cache, and a stale hit reported a retired id as
            # the duplicate while no active entry held the content.
            cached = self._l0_get(entry.content_hash)
            if cached is not None:
                live = conn.execute(
                    "SELECT 1 FROM memory_entries WHERE id = ? AND status = 'active'",
                    (cached,),
                ).fetchone()
                if live is not None:
                    return DedupResult(outcome="duplicate_l0", entry_id=cached)
                self._l0_cache.pop(entry.content_hash, None)

            # L1: DB content_hash check
            row = conn.execute(
                """SELECT id FROM memory_entries
                   WHERE content_hash = ? AND status = 'active'
                   LIMIT 1""",
                (entry.content_hash,),
            ).fetchone()
            if row is not None:
                self._l0_put(entry.content_hash, row["id"])
                return DedupResult(outcome="duplicate_l1", entry_id=row["id"])

            # Tier 1: same (resource_name, entry_type) overlap
            overlaps = conn.execute(
                """SELECT id FROM memory_entries
                   WHERE resource_name = ?
                     AND entry_type = ?
                     AND status = 'active'
                   ORDER BY updated_at DESC
                   LIMIT 5""",
                (entry.resource_name, entry.entry_type),
            ).fetchall()
        finally:
            conn.close()

        related_ids = [r["id"] for r in overlaps]
        if related_ids:
            return DedupResult(outcome="related", related_ids=related_ids)
        return DedupResult(outcome="new")

    def record_new(self, entry: Entry) -> None:
        """After a successful insert, populate the L0 cache."""
        self._l0_put(entry.content_hash, entry.id)

    # ---- helpers ----------------------------------------------------------

    def _l0_get(self, key: str) -> str | None:
        try:
            value = self._l0_cache[key]
        except KeyError:
            return None
        self._l0_cache.move_to_end(key)
        return value

    def _l0_put(self, key: str, value: str) -> None:
        if key in self._l0_cache:
            self._l0_cache.move_to_end(key)
            self._l0_cache[key] = value
            return
        self._l0_cache[key] = value
        while len(self._l0_cache) > self._l0_capacity:
            self._l0_cache.popitem(last=False)


def make_related_edges(
    new_entry_id: str, related_ids: list[str]
) -> list[Relation]:
    """Build RELATED edges between a new entry and the Tier-1 overlaps."""
    now = datetime.now(UTC).isoformat()
    return [
        Relation(
            src_id=new_entry_id,
            dst_id=other_id,
            relation_type="RELATED",
            metadata={"reason": "tier1_resource_overlap"},
            created_at=now,
        )
        for other_id in related_ids
    ]
