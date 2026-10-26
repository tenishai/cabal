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
      L0  in-memory LRU cache on content_hash (skip if hit). A hit
          counts only while the cached entry is still active (cabal#353).
      L1  DB content_hash check against active entries (skip if hit)
      Tier 1  find existing entries with same (resource_name,
              entry_type); record RELATED relation on near-overlap
      Tier 2  deferred (LLM SUPERSEDES/CONTRADICTS/SUPPLEMENTS)
  - publish runs in one BEGIN IMMEDIATE transaction, from the dedup
    check through the insert and any supersede, so racing writers
    cannot both insert the same content (cabal#358).
  - Deterministic supersession: publish(supersede=True) retires the
    active entries it replaces (status='superseded', SUPERSEDES edge).
    supersede_scope="type" (default) retires same resource_name and
    entry_type; "resource" retires every type under the name (cabal#350).
  - Search via direct FTS5 on the archivist's own table, active
    entries only; every query token is quoted, so the query is plain
    words (cabal#356).
  - Cross-reference edges (RELATED, SUPERSEDES) via memory_relations.
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
