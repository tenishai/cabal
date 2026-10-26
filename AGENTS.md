# AGENTS.md — cabal

Instructions for AI coding agents working in this repo. Claude Code loads this via
`CLAUDE.md` (`@AGENTS.md`); other AGENTS.md-aware tools read it directly.

cabal is orchestration: a FastAPI broker that dispatches work between agents over A2A,
with JWT-scoped auth, a bulletin board, and SQLite underneath.

To explain cabal to a user, run `cbl guide welcome`: it ships in the package
(`src/cabal/guides/`) and describes the installed version. Keep it true when you change
an option it covers.

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

Without liminal, the handler tests import a vendored copy of `liminal.protocol` from
`tests/_vendor`. When liminal's `protocol` package changes, re-copy it there and bump
`VENDORED_FROM` in `tests/_vendor/liminal/__init__.py`;
`tests/test_vendored_liminal_protocol.py` fails against an installed liminal otherwise.

## Dispatch-token policy lives in one place

`src/cabal/core/routes/_dispatch_auth.py` decides who may dispatch. `dispatch_token`
applies the scope, `sub` and `dispatch_pairs` rules (D-018 in `CABAL-DECISIONS.md`), and
`dispatch_audience` gives a federated target's token its `federation_audience`. The REST
route (`routes/tasks.py`), the legacy JSON-RPC route (`routes/a2a_jsonrpc.py`) and the
A2A v1.0 route (`routes/a2a_v1.py`) all call it, and only translate a refusal into their
own wire form. Change the policy there, not in a route: the doors used to carry their own
copies, and those drifted (#234).

The TUI's in-process client (`tui/client/inprocess.py`) keeps its own check: a token
already aimed at the persona's audience (from `dispatch_audience`: a federated persona's
`federation_audience`, otherwise its name) is used as it is, and only operator scope may
re-mint.

Related and open: the re-mint uses a hard-coded `scope=["dispatch"]`,
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
  archivist/     shared-memory sync (gitmemory, gitsync, dedup, promote, store,
                 purge: admin-only, CLI)
  guides/        markdown guides bundled in the wheel, printed by `cbl guide`
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
cite the original Cabal design document, which is not in this repo (#202). Do not treat a proposal as a spec
without checking it against the code.

## Architecture cards

`ARCHITECTURE.md` is the repo card: what this repo is, its entry points, and an index of its seams. Each seam that has a card lists it under `SEAMS → card:`, pointing at `docs/arch/<seam>.md`. Read the card before changing the code it describes.

Every card pins the file it describes with an `anchor: <path> sha=<git blob sha>` line in its frontmatter. When you change an anchored file, update the card in the same change: fix the text, set `updated:`, and replace the sha with `git hash-object <path>`. To add a seam card, add its line to the `SEAMS → card:` index too.

Check before you push:

```
python3 scripts/check_arch_cards.py
```

It fails when an anchored file has changed without its card, or when a card and the index disagree.
