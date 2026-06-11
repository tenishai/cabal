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

### D-012: container builds use BuildKit `additional_contexts` for techne

- **Question**: How does the Dockerfile get techne in without a PyPI publish?
- **Choice**: Use Compose v2's `additional_contexts` to expose `../techne` as a
  named build context (`techne`); the Dockerfile's build stage `COPY --from=techne`
  pulls in the source and `pip install`s it into the runtime virtualenv.
- **Reasoning**: Mirrors D-008 (sibling-directory convention). Requires the
  operator who runs `docker compose build` to have `../techne` checked out, which
  is the same assumption local dev already makes. Zero new infrastructure.
  Discarded alternatives: bind-mounting techne as a runtime volume (breaks the
  "image is self-contained" invariant), vendoring into the cabal repo (release
  cadences couple).
- **Public-repo prerequisite**: SAME as D-008. When techne hits PyPI:
    - `pyproject.toml` adds `techne>=0.6.0` to `dependencies`,
    - Dockerfile drops the `COPY --from=techne` step (pip handles it via PyPI),
    - `docker-compose.yml` drops `additional_contexts.techne`,
    - This decision marked SUPERSEDED.
- **Lands in**: `Dockerfile` (COPY + pip install), `docker-compose.yml` build block.

### D-013: container CMD via cbl, not uvicorn directly

- **Question**: Does the container CMD invoke `uvicorn cabal.core.app:create_app`
  or `cbl daemon up`?
- **Choice**: `cbl daemon up`
- **Reasoning**: `cbl daemon up` already wraps uvicorn + the right defaults
  (host 0.0.0.0, port from --port, --config). Going through it means the
  container behaves identically to the local dev daemon — same logs, same
  startup sequence, same `--agent` semantics for scale mode. If we ever
  change daemon-bring-up behavior, the container picks it up for free.
- **Lands in**: Dockerfile CMD.

### D-014: trace export defaults to async, never blocks dispatch

- **Question**: Should the TraceExporter handlers run sync (blocking the
  request) or async (fire-and-forget)?
- **Choice**: async by default; sync available for tests.
- **Reasoning**: Observability is a "nice to have" relative to a working
  dispatch. A failing Langfuse endpoint should not delay a Task. Async means
  the handler runs in a fire-and-forget task whose exceptions are logged but
  never propagate. Tests pass mode="sync" for deterministic assertion of
  recorded events.
- **Lands in**: `core/observability/trace_exporter.py::TraceExporter.mode`.

### D-009: events.id Integer (not BigInteger) for SQLite autoincrement

- **Question**: SQLite only autoincrements `INTEGER PRIMARY KEY`, not
  `BIGINT PRIMARY KEY`. The handoff doc schema specifies `BIGINT` for
  `events.id`. Stick with BIGINT and hand-roll autoincrement, or downgrade?
- **Choice**: Integer
- **Reasoning**: SQLite's INTEGER is a 64-bit signed int internally —
  same capacity as a "BIGINT" on any other DB. We pay zero cost. Switching
  to BIGINT for PostgreSQL portability when we get there is a single-line
  migration. The cost of NOT fixing it: autoincrement silently broken on
  the very first event insert, which is what failed the broker tests.
- **Lands in**: `models.Event.id`, migration 0001.

### D-010: Operator tokens transparently re-minted per-target in /a2a/tasks

- **Question**: When an operator hits POST /a2a/tasks with their
  `aud=cabal-core, scope=["operator"]` token, should the endpoint reject
  it (force the operator to pre-mint a per-target token via a separate
  call), or re-mint internally?
- **Options**:
  - A. Reject; force the operator to call POST /auth/mint-dispatch first.
  - B. Re-mint internally; operator's UX stays simple.
- **Choice**: B
- **Reasoning**: Pre-minting per-target tokens adds a round-trip on every
  dispatch with no security benefit — Cabal Core would be the only entity
  signing both tokens anyway. Re-mint internally keeps the wire-level
  invariant (every dispatch carries a target-scoped JWT) without making
  operators feel it. Non-operator tokens with mismatched aud still get a
  hard 403 — only operators are special.
- **Lands in**: `routes/tasks._resolve_dispatch_token`.

### D-011: Circuit breaker placement (client wrapper, not LiteLLM proxy)

- **Question**: Where do we host the LiteLLM circuit breaker?
- **Options**:
  - A. Inside LiteLLM proxy config — simpler, no client code.
  - B. At Cabal's LiteLLM client wrapper — duplicates LiteLLM's own breaker
    if any, but gives us per-call observability via hooks.
- **Choice**: B
- **Reasoning**: Per §6.11 of the handoff doc: when the proxy errors
  cluster, we want every persona to back off TOGETHER. A breaker at the
  proxy only tells the proxy itself; a breaker at our client lets us emit
  the `circuit_open` event into our own audit log + hook system, which
  the agents can use to gracefully 704 instead of spinning. Also: the
  HALF_OPEN single-probe semantics §6.11 specifies are easy to write at
  our layer, fiddly to extract from LiteLLM internals.
- **Lands in**: `core/llm/circuit_breaker.py`, `core/llm/litellm_client.py`.

### D-008: techne installed from `../techne` sibling checkout (durable)

- **Question**: techne is not on PyPI. How should cabal depend on it?
- **Choice**: Sibling-directory convention. Anyone running cabal locally is expected
  to have `dimartin/techne` cloned as a sibling of `cabal` (`../techne`). Cabal's
  `pyproject.toml` does not list techne; the README's dev quickstart adds
  `pip install -e ../techne` as a separate step. The Dockerfile uses BuildKit
  additional contexts to install techne the same way during image builds.
- **Reasoning**: Sibling layout is the simplest durable answer — works for local
  dev, docker compose builds, and anyone cloning fresh. Avoids hand-rolling a
  private package server (Gitea PyPI was considered and rejected in 2026-06-11
  conversation: extra moving piece for solo use). Pin via versioned dep is
  unnecessary while both repos share a single operator.
- **PUBLIC-REPO PREREQUISITE**: Before the first public Cabal release, this
  decision flips. Public release requires:
    - techne published to PyPI (or any pip-installable index) under
      `techne>=0.6.0`,
    - Cabal `pyproject.toml` adds `techne>=0.6.0` to `dependencies`,
    - Cabal `Dockerfile` reverts to a plain `pip install` step (drop the
      additional-context BuildKit dance),
    - README quickstart drops the sibling-clone instruction,
    - This decision marked SUPERSEDED with a pointer to the new PyPI-anchored
      decision and the day it landed.
  See `STATUS.md` § "Pre-public-release checklist" — that's the running
  reminder.
- **Lands in**: `README.md` quickstart, `Dockerfile` build stage, `docker-compose.yml`
  `additional_contexts`, this log.

### D-007: Cryptography library for AgentCard signing

- **Question**: cryptography vs. Authlib vs. python-jose?
- **Choice**: Authlib + cryptography (Authlib uses cryptography under the hood)
- **Reasoning**: Authlib gives us JWT + JWS in one library and is what we want
  for the bootstrap-code/JWT flow anyway. python-jose has a long unmaintained
  fork history; cryptography alone means we'd implement JWS framing ourselves.
- **Lands in**: `pyproject.toml` deps.
