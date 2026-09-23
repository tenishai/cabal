# AGENTS.md — cabal

Instructions for AI coding agents working in this repo. Claude Code loads this via
`CLAUDE.md` (`@AGENTS.md`); other AGENTS.md-aware tools read it directly.

cabal is orchestration: a FastAPI broker that dispatches work between agents over A2A,
with JWT-scoped auth, a bulletin board, and SQLite underneath.

## Build, test, lint

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
mypy src
```

Both `[tool.ruff]` and `[tool.mypy]` are configured in `pyproject.toml`, and **neither has
a Makefile target** — run them directly. `make test-unit` and `make test-integration`
currently run the identical `pytest -q`, so the distinction the names imply does not
exist; do not read a passing `test-integration` as integration coverage.

The unit suite needs no siblings. Tests that exercise techne or liminal want those
checked out adjacent (`pip install -e ../techne`), and without them you get import errors
that look like real failures.

## Two doors, not one — the trap most likely to bite

`_resolve_dispatch_token` is implemented **twice**:

- `src/cabal/core/routes/tasks.py:59`
- `src/cabal/core/routes/a2a_jsonrpc.py:62` (whose own comment says it mirrors the other)

Any change to dispatch-token policy must touch both, or the JSON-RPC door silently keeps
the old behaviour and reopens whatever the REST door just closed. `routes/directory.py:5`
points at the same pair. This is recorded as D-018 in `CABAL-DECISIONS.md`.

Related and open: `_resolve_dispatch_token` re-mints with a hard-coded `scope=["dispatch"]`,
so the caller's `operator` scope does not survive into the router (#164). A router-level
guardrail cannot ask "is this the operator?" the way the rest of the codebase does.

## Conventions that look like mistakes and are not

- **Renames keep the old name in artifacts that asserted it.** rook→mason and
  jackdaw→wright are done in indexes and current prose, but a test docstring that recorded
  `rook-implementer` at the time keeps it. `STATUS.md`'s build-arc table states the rule. A
  mechanical rename sweep across the repo breaks this deliberately-preserved distinction.
- **Migrations ship as package data** (`README.md:44`, cabal#79). Adding an Alembic
  revision under `src/cabal/migrations/versions/` is also a packaging change — check the
  package-data glob still picks it up.
- **`identity.toml` is gitignored**; `identity.toml.example` is the template.
  `identity.mtplx.toml` is tracked but is a *personal* worked example, not a second
  template (#206). Asked to "add a persona example", edit the `.example` file.

## Directory boundaries

```
src/cabal/
  core/          the substrate: FastAPI app, A2A broker, auth, routes, observability
  agents/        persona implementations (LLMPersona, EchoAgent, personas/mason.py)
  handlers/      liminal-facing MCP handlers — archivist, code, documents, patterns
  archivist/     shared-memory sync (gitmemory, dedup, promote, store)
  librarian/     document indexing + adapters
  codex/         code-graph blame and repo resolution
  cli/           the `cbl` entry point
  tui/           the `cabal` entry point — a Textual terminal UI
  migrations/    Alembic revisions (package data)
```

`core/` and `handlers/` look adjacent and serve **different protocols**: `core/` is A2A
and the broker; `handlers/` are MCP entry points registered through
`[project.entry-points."liminal.handlers"]` in `pyproject.toml`, consumed by liminal.
Dispatch logic added to a handler is in the wrong layer.

## Docs

`docs/architecture.md` is the current description. `docs/proposals/` are design notes, not
documentation of current behaviour — several are stale or superseded (#205), and several
cite a `docs/cabal.md` that does not exist (#202). Do not treat a proposal as a spec
without checking it against the code.
