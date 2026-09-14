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
- **Reasoning**: techne already targets 3.12 (matches `../techne/pyproject.toml`).
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
- **Amended (colleague-scope work)**: "only operators are special" no longer
  holds. `colleague` is now a second re-minting scope on the same function, and
  on its JSON-RPC twin `a2a_jsonrpc._resolve_dispatch_token`. The decision
  itself — re-mint internally rather than force a pre-mint round-trip — stands;
  only the list of scopes it applies to grew.

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

### D-015: secrets table activated; env vars override DB for one-off testing

- **Question**: Where do credentials for credentialed handlers (slack, github,
  etc.) live? The `secrets` table has been in the schema since Phase 1 unused;
  the env-var path (`CABAL_SECRET_<persona>_<handler>_<KEY>`) shipped in
  Phase GG.
- **Options**:
  - A. Env vars only — simple, but doesn't persist across daemon restarts
    unless the operator wraps the daemon in a startup script.
  - B. DB only — durable + hot-updatable, but env vars are how operators
    naturally test "what if I set this to X for one run."
  - C. DB-as-default + env vars override.
- **Choice**: C
- **Reasoning**: The encrypted-at-rest path (Fernet with the existing
  master_key, sibling to signing-key encryption) is the canonical durable
  surface — `cbl secret set archivist slack SLACK_BOT_TOKEN` persists, is
  hot-readable on the next request, and survives daemon restarts. Env vars
  ride on top as a quick-override shim so an operator pasting a value into
  a shell for one-off testing doesn't have to wipe DB state. The merge
  contract (env wins) makes the precedence predictable.
- **Naming**: secret rows keyed by `<persona>:<handler>:<KEY>` (colon
  separator). Colons because multi-underscore handler names + multi-
  underscore KEYs would collide if we'd used underscores like the env-var
  path does (the env-var endpoint splits on a known prefix; the DB
  doesn't have that anchor). KEY must match `^[A-Z][A-Z0-9_]*$` so it
  lines up with capability_card.credential_keys conventions.
- **Lands in**: `core/secrets.py`, `core/routes/admin.py::set_secret /
  unset_secret / list_secrets`, `cli/main.py::secret_app`.

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

---

## 2026-08-01 — primus rename (rook → mason, jackdaw → wright)

### D-016: persona-scoped secrets are NOT migrated across the rename

- **Question**: Renaming a persona changes the key its secrets live under.
  Secrets are keyed `<persona>:<handler>:<KEY>` in the `secrets` table
  (`core/secrets.py:36-38`) and mirrored by env vars
  `CABAL_SECRET_<persona>_<handler>_<KEY>`. After `rook` → `mason`, an
  operator's `rook:slack:TOKEN` row and their `CABAL_SECRET_rook_slack_*`
  env vars become unreachable — silently. No error, no dangling FK
  (`secrets.name` is a bare string PK); just an absent credential and a
  handler that quietly does not work. Do we migrate?
- **Options**:
  - **Migrate the DB half** — an `UPDATE secrets SET name = replace(...)`
    rewriting the persona prefix.
  - **Migrate nothing; document it** — operators re-key both surfaces.
  - **Migrate and warn** — rewrite the DB, log for the env half.
- **Choice**: migrate nothing; document it (this entry + the PR body).
- **Reasoning**:
  - **The env-var half cannot be migrated at all.** It lives in the
    operator's shell/systemd unit/k8s manifest, not in any store we own.
  - So a DB-only migration fixes *half* the surface and leaves an
    inconsistent state: some credentials follow the rename, others do not,
    and the operator cannot tell which surface a given credential came from.
    **A partial migration is worse than none** — it converts a uniform,
    explainable break into a per-credential mystery.
  - It would also be a rewrite of the PRIMARY KEY of encrypted credential
    rows, with a collision mode if both old and new keys exist. High blast
    radius, low reversibility, for tidiness.
  - Same session, the sibling migration `b7d3f1a2c9e4` (dropping orphaned
    `personas` rows) was written, found to orphan `agents` FK rows, and
    dropped. The lesson generalises: prefer a documented break to a clever
    migration when the mechanism is not fully under our control.
- **Landed**: no migration. Operator action required on upgrade —
  re-key DB secrets (`cbl secret set`) and rename env vars from
  `CABAL_SECRET_rook_*` / `CABAL_SECRET_jackdaw_*` to `CABAL_SECRET_mason_*`
  / `CABAL_SECRET_wright_*`. Raised by Prudence on PR #113.

### D-017: authenticated-but-wrong-audience is 403, uniformly

- **Context**: consolidating the admin guards into `_require_scope`
  (colleague-scope work) surfaced an untested edge: a validly signed
  token presented to a guard whose audience it does not match
  previously fell out as 401 on the two pre-existing guards.
- **Landed**: 403 for every authenticated-but-wrong-realm case, on
  all guards. The rule is now: no or invalid signature is 401;
  authenticated but wrong scope OR wrong audience is 403. Nothing in
  the repo branched on the old 401 (CLI and TUI use blanket 4xx
  handling); the dispatch gate already said 403 for its mismatched
  cases.

## 2026-08-22 — dispatch pairs: the switchboard checks who may call whom

### D-018: dispatch_pairs is a flat allow-list, room-owned, empty means open

- **Question**: agent-to-agent dispatch (a caller whose JWT `sub` is
  `agent:<name>` re-minting toward a target via `POST /a2a/tasks`, see
  D-010) had no policy layer at all beyond "does a signing key exist for
  this token". Once more than a couple of agents can reach each other,
  something has to decide which pairs are allowed. Where does that
  decision live, and what does cabal store?
- **Options**:
  - A. Cabal computes the policy itself (roles, capability tags, graph
    rules) and exposes an admin UI for it.
  - B. Cabal stores nothing; the caller re-mint path stays wide open for
    any `agent:` sub and policy is enforced upstream, before the request
    ever reaches cabal.
  - C. Cabal stores a flat, opaque `(caller, target)` allow-list it does
    not interpret, synced wholesale by an external owner, and enforces
    it at the one re-mint chokepoint it already owns.
- **Choice**: C
- **Reasoning**: Cabal has no model of "teams", "trust levels", or
  anything else that would let it *compute* an allow-list — that
  knowledge lives in the room (kobo) that actually assembles and
  supervises agents. Option A would duplicate that model badly inside
  cabal and get it wrong. Option B leaves the one place that can
  actually enforce anything (the re-mint chokepoint every agent-to-agent
  dispatch passes through) doing nothing, which only works as long as
  nobody malicious or buggy ever gets a JWT. Option C keeps cabal dumb
  on purpose: it stores exactly the pairs it's told, replaces the whole
  set atomically on every sync (`PUT /admin/dispatch-pairs`, operator
  scope), and answers exactly one question at dispatch time — "is
  (caller, target) in the set" — without ever knowing *why* a pair
  exists.
- **Empty table means open, by design**: a table with zero rows is not
  an error state or "misconfigured" — it's the default, and it means
  every `agent:` caller may reach every target. This is what makes the
  feature deploy-safe: the migration ships and rolls out with an empty
  table, dispatch behavior is unchanged, and enforcement only begins the
  first time the room performs its first sync (even syncing an empty set
  explicitly is different from never having synced — see caveat below).
  The alternative (empty-means-closed, or a separate "enforcement
  enabled" flag) would make the migration itself a breaking change: every
  agent-to-agent dispatch in flight at deploy time would start failing
  the instant the table exists, with no coordinated cutover possible.
- **Atomic replace, not incremental add/remove**: `PUT` deletes every
  existing pair and inserts the new set inside a single transaction
  (`dispatch_pairs.replace_all`, one `open_session` block). This is not
  just tidiness — because empty means open, a non-atomic replace (delete
  everything, *then* insert, as two visible steps) would create a real
  window where the table is briefly empty and every dispatch is
  momentarily open again. Proven in `tests/test_dispatch_pairs.py` by
  literally interleaving a paused writer with a reader on a separate
  connection.
- **Caveat carried over from D-015's lesson**: the room is expected to
  push its full current policy on every sync, not deltas. Cabal does not
  track "has anyone ever synced" as a separate bit from "the table has
  rows" — an operator (or the room, mid-incident) manually clearing the
  table with an empty `PUT` re-opens dispatch exactly like a fresh
  install would. That's intentional (matches "empty means open"
  uniformly) but worth knowing before reaching for it as a kill switch.
- **What this does NOT do**: gate on any JWT scope claim (e.g. a
  `colleague` scope). The enforcement point keys purely on the `sub`
  prefix (`agent:`) plus the pairs table, deliberately independent of
  how a given agent-sub token came to exist. This repo's colleague-scope
  work (PR #124, `feat/colleague-scope`) was still open/unmerged as of
  this entry — this feature does not depend on it landing first, and
  will keep working unchanged once it does (a `colleague`-scoped
  `agent:` token hits the exact same gate as any other `agent:` token).
- **Revocation lag**: revoking a pair does not revoke a token already in
  a caller's hand. `_resolve_dispatch_token`'s fast path — a token whose
  `aud` already matches the target — returns that token as-is without
  ever consulting `dispatch_pairs` (see the top of the function). So a
  caller that re-minted a per-target token *before* the room removed its
  pair keeps dispatching against that target for the rest of the token's
  TTL (`DEFAULT_TTL_SECONDS` in `core/auth/jwt.py`, five minutes by
  default), even though a fresh re-mint attempt would now be refused.
  This is a property of re-mint tokens generally, not something this
  feature could close without re-checking pairs on every single request
  (which would defeat the point of re-minting at all). The room's UI
  should say so wherever it shows a pair as "revoked" — the effective cut
  over can lag up to one token TTL behind the sync.
- **Empty-resync blast radius**: because empty means open, a `PUT` with
  `pairs: []` does not "lock everything down" — it does the opposite,
  reopening dispatch for every `agent:` caller against every target, same
  as a table that was never synced. Shrinking a large policy down to
  `[]` (a bug in the room's diffing logic, a bad merge, an accidental
  full clear) is indistinguishable to cabal from a deliberate "open
  everything back up" instruction — cabal stores what it's told and
  nothing more. The room-side client that owns this sync is expected to
  guard against sending `[]` unintentionally (e.g. treat a sync that
  would shrink the set to zero as suspicious and require confirmation)
  and to log loudly whenever it does send one, since cabal itself has no
  way to tell "the room meant to reopen everything" apart from "the
  room's policy computation returned nothing by mistake".
- **Lands in**: `core/models.py::DispatchPair`,
  `core/dispatch_pairs.py`, `core/routes/tasks.py::_resolve_dispatch_token`,
  `core/routes/admin.py::get_dispatch_pairs / put_dispatch_pairs`,
  migration `c4f0a2b18e77`.
