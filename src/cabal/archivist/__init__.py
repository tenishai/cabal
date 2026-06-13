"""Archivist — team consensus knowledge store.

The Archivist is the system of record for everything the team has
learned, decided, observed, or done. Distinct from the other
informational personas because it's the only one that's multi-writer
with conflict resolution.

MVP scope (cabal#9): the schema + the 4-layer dedup pipeline minus
Tier-2 (LLM classification deferred to a follow-up that wires the
LLMClient through). What ships today:

  - Per-entry schema: resource_name, summary, entry_type, tags,
    details, gotchas, commands, refs, importance, source_thread,
    content_hash, written_by, status, access_count, timestamps.
  - 4-layer dedup pipeline minus the LLM verdict:
      L0  in-memory LRU cache on content_hash (skip if hit)
      L1  DB content_hash check (skip if hit)
      Tier 1  find existing entries with same (resource_name,
              entry_type); record RELATED relation on near-overlap
      Tier 2  deferred (LLM SUPERSEDES/CONTRADICTS/SUPPLEMENTS)
  - Search via techne hybrid recall.
  - Cross-reference edges (RELATED) via memory_relations table.
  - schema() introspection so agents can self-discover the corpus.

Deferred (called out in the handler docstring as follow-ups):
  - Tier-2 LLM classification (needs LLMClient wiring + an async
    worker pattern; sync stub returns "UNRELATED" today)
  - Temporal knowledge graph (graph_nodes + graph_edges with
    valid_from/valid_until; separate subsystem)
  - Postgres backend (SQLite-only for v1)
  - Background write-worker (sync write path for v1)

The Archivist owns its own SQLite database — entries are too richly
schemaed to live as `techne.knowledge` rows. The corpus is searched
via direct FTS5 on its own table.
"""

from cabal.archivist.dedup import Dedupper, DedupResult
from cabal.archivist.models import Entry, EntryType, Relation, RelationType
from cabal.archivist.store import ArchivistStore

__all__ = [
    "ArchivistStore",
    "DedupResult",
    "Dedupper",
    "Entry",
    "EntryType",
    "Relation",
    "RelationType",
]
