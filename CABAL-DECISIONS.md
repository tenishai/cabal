# Cabal Decisions Log

Running log of autonomous-mode decisions made during overnight build sessions.
Each entry: question, options considered, choice, reasoning, where it landed.
Reviewed by Richard the morning after.

Linked feedback memory: `feedback-decision-authority`.

---

## 2026-06-10 — Phase 1 build kickoff

### D-001: Python version

- **Question**: What Python version do we target for Cabal Core?
- **Options**:
  - 3.11 — widely available, slightly older async ergonomics
  - 3.12 — current stable, type-param syntax, improved error messages
  - 3.13 — latest, free-threading still experimental, some deps lag
- **Choice**: 3.12
- **Reasoning**: techne already targets 3.12 (matches `infra-techne/pyproject.toml`).
  3.13 free-threading isn't useful for us (we're I/O-bound) and some libraries
  (notably anything pinning a C-extension wheel) still lag. 3.11 forfeits the
  `type` statement and PEP 695 generics for no real gain.
- **Lands in**: `pyproject.toml requires-python = ">=3.12"`, `.python-version`.

### D-002: Web framework

- **Question**: FastAPI vs. Starlette + manual routing vs. Litestar?
- **Options**:
  - FastAPI — dominant, OpenAPI auto-gen, pydantic-first, large surface
  - Starlette + manual — lean, no docs gen, more code to write
  - Litestar — fast-growing, cleaner DI, smaller community
- **Choice**: FastAPI
- **Reasoning**: A2A is JSON-RPC-shaped; FastAPI's pydantic integration is the
  cleanest path to validated A2A Task envelopes. OpenAPI auto-gen pays for itself
  the moment Architect tries to call us via MCP-as-A2A-client. Litestar would be
  a contrarian bet; not worth it on a personal platform.
- **Lands in**: `pyproject.toml` deps, `src/cabal/core/app.py`.

### D-003: SQLite ORM vs. raw

- **Question**: SQLAlchemy 2.0 vs. raw `sqlite3` + lightweight wrapper?
- **Options**:
  - SQLAlchemy 2.0 (async) — full ORM + migrations via Alembic
  - aiosqlite + hand-written queries
  - sqlmodel — SQLAlchemy + pydantic blend
- **Choice**: SQLAlchemy 2.0 async
- **Reasoning**: We need Alembic for schema migrations (already specified in
  handoff doc §5.1). Alembic is built around SQLAlchemy metadata. Going raw
  means writing migration scaffolding ourselves. sqlmodel is appealing but its
  release cadence has been spotty; SQLAlchemy 2.0 core is rock solid.
- **Lands in**: `pyproject.toml` deps, `src/cabal/core/db.py`.

### D-004: CLI framework

- **Question**: Typer vs. Click vs. argparse?
- **Options**:
  - Typer — type-hint-driven, built on Click, FastAPI sibling
  - Click — battle-tested, more verbose
  - argparse — zero deps, ugly for nested subcommands
- **Choice**: Typer
- **Reasoning**: `cbl` will grow nested subcommands (`cbl primus rotate`,
  `cbl agent register`, `cbl token mint`, etc.). Typer's type-hint-driven
  signatures keep the CLI surface readable. FastAPI sibling, so deps are
  already in the dependency closure via Click.
- **Lands in**: `pyproject.toml` deps, `src/cabal/cli/main.py`.

### D-005: Async runtime

- **Question**: asyncio (stdlib) vs. trio vs. anyio?
- **Choice**: asyncio (FastAPI mandates it)
- **Reasoning**: No reasonable alternative — FastAPI/Starlette is asyncio-native.
- **Lands in**: package-wide.

### D-006: Test framework

- **Question**: pytest + pytest-asyncio
- **Choice**: pytest + pytest-asyncio
- **Reasoning**: standard for the FastAPI ecosystem; matches techne's choice.
- **Lands in**: `pyproject.toml [project.optional-dependencies] dev`.

### D-008: techne dependency strategy (0.6.0 not yet on PyPI)

- **Question**: techne 0.6.0 isn't published to PyPI. How should cabal depend on it?
- **Options**:
  - A. `techne @ file:///Users/dimartin/working/claude/infra-techne` in pyproject —
    pins a path that won't work on other machines.
  - B. Drop techne from `dependencies`, install it separately from the local checkout
    during dev. Add an import-time guard in any module that needs techne.
  - C. Publish techne 0.6.0 to PyPI tonight.
- **Choice**: B
- **Reasoning**: A is brittle (path-dependent, breaks CI later). C is a real action
  but requires PyPI account setup, README polish for first public release, and a
  decision about whether to make techne public — none of which are right calls to
  bake into tonight's autonomous run. B is the minimal step that lets us proceed:
  pyproject documents the soft dependency in a comment; the cabal venv has a
  local-editable techne install; the day we publish techne we add the pin back.
- **Lands in**: `pyproject.toml` comment block, dev-setup section of README,
  manual `pip install -e ../infra-techne` step in venv bootstrap.

### D-007: Cryptography library for AgentCard signing

- **Question**: cryptography vs. Authlib vs. python-jose?
- **Choice**: Authlib + cryptography (Authlib uses cryptography under the hood)
- **Reasoning**: Authlib gives us JWT + JWS in one library and is what we want
  for the bootstrap-code/JWT flow anyway. python-jose has a long unmaintained
  fork history; cryptography alone means we'd implement JWS framing ourselves.
- **Lands in**: `pyproject.toml` deps.
