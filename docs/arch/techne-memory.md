---
card: arch-card:cabal-techne-memory
updated: 2026-10-07
anchor: src/cabal/core/memory/store.py sha=6df8b3ccb3c928e72bb19fee6dafcf87e834e9b0
anchor: src/cabal/core/memory/techne_store.py sha=9ef15bc619632226473e9cb74c5285b7ae7c6a21
anchor: src/cabal/core/memory/session_tracker.py sha=f59095cdc63eca0cc05dd1bc0227969ea9de457c
---
# techne-memory — persona memory and session tracking on techne

WHAT / SEAM: `MemoryStore` Protocol (core/memory/store.py: sync `recall(query, limit) -> list[MemoryHit]`, `commit(content, type, tags, confidence)`); impls `NoOpMemoryStore` (default) and `TechneMemoryStore` (techne_store.py). `SessionTracker` (session_tracker.py: `start(external_id, agent_identity)`, `end(session_id, summary)`); impls NoOp and `TechneSessionTracker`.
TECHNE API SURFACE CABAL DEPENDS ON (change any of these in techne and cabal breaks): `techne.init_db`, `techne.search.search(db, q, table="knowledge", limit, use_vectors)`, `techne.knowledge.store(db, content, type, tags, confidence, source="cabal")`, `techne.Config(db_path, data_dir, platform)`, `techne.lifecycle.init_session_for_external_id`, `techne.lifecycle.run_session_end`, `techne.core.database.connect` (librarian/store.py, codex/repos.py), `techne.core.patterns.{search_patterns, upsert_investigation, add_pattern}` (handlers/patterns.py).
OPS / FLOW: core/app.py lifespan: if `[techne].db_path` set → resolve under the agent data dir (a RELATIVE path moved there from CWD-relative; an old deployment silently gets a fresh empty DB) → `techne.init_db` if missing → build TechneMemoryStore + TechneSessionTracker → passed to every persona and subordinate by persona_factory.
INVARIANTS:
- techne is OPTIONAL (`tenishai-cabal[memory]` extra, `techne>=0.18.0`); every import is lazy. WHY: `import cabal` and the unit suite must work without the sibling.
- Every techne call is wrapped and degrades: recall → [], commit / session start / end → log warning (start returns the external_id). WHY: memory and observability must never block dispatch. Consequence: a broken techne install shows up only as `memory.*_failed` / `session.*_failed` log lines. The `import techne` in `TechneMemoryStore.recall`/`commit` now sits INSIDE the try, so `[techne].db_path` set without techne installed degrades to no hits / a warning rather than raising (app.py still builds the store when `techne.init_db` failed to import, so the only signal is the log line).
- `_sanitize_query` strips FTS5 specials and stopwords and ORs the tokens before search. WHY: raw user text breaks FTS5 MATCH syntax.
- Protocol methods are SYNC and call SQLite directly (not via a thread). Async callers block the event loop for the query's duration.
- Separate stores: the Archivist's team memory is its own SQLite DB (cabal/archivist/, `CABAL_ARCHIVIST_DB`), NOT techne; the Librarian and Codex write techne knowledge rows (type="document", tag kind:code); the Architect's patterns live in techne's `architect.db`.
CONSUMERS: agents/llm_persona.py (`memory_store.recall`, `memory_store.commit`, both called inline from async code), agents/subordinate.py, core/persona_factory.py, core/app.py; POST /admin/reload/identity re-uses `app.state.memory_store` (routes/admin.py `reload_identity`).
