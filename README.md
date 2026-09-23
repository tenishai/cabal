# Cabal

Self-hosted personal agent orchestration platform. Cabal Core is the SQLite-backed
control plane and A2A broker; personas (Architect, Archivist, Librarian, ...) and
the Mason primus run as agent modules registered with the bulletin board.

## Status

**The suite passes.** All phases through the security + hygiene sweep are complete:
trust boundary enforcement, streaming dispatch router, techne correctness, async
hygiene, SSRF defense, CI on all three repos, MCP spec compliance, and a full
hygiene pass. The end-to-end demo (`make demo`) runs cleanly with Docker Compose.

## What works

- **Full A2A dispatch loop** — submit a task via HTTP or CLI, get a completed
  Task back. In-process local dispatch and HTTP remote dispatch both work.
- **JWT trust boundary** — audience + scope + RS256 algorithm pin enforced on
  every inbound call. Operator tokens cannot impersonate liminal service tokens
  and vice versa. `allow_dev_master_key` defaults to `False`.
- **liminal integration** — personas that have `[mcp]` wired in their identity.toml
  persona block get a `tool_proxy` that dispatches to liminal over JSON-RPC. The
  credential flow (`cbl secret set` → `/admin/secrets` → `ctx.credentials` in the
  handler) is complete and tested end-to-end.
- **LLM personas** — `LLMPersona` wraps LiteLLM acompletion with tool-use loop,
  budget guard, circuit breaker, and per-step hook callbacks.
- **CLI** — `cbl` covers daemon, login, dispatch, token mint/list, secret set,
  persona reload, agent status.
- **Docker Compose dev stack** — `make up / bootstrap / demo` runs the full
  cabal + liminal stack; `make demo` is a 6-step smoke test that passes reliably.

## Layout

```
src/cabal/
  core/           FastAPI + SQLite + A2A broker + bulletin board
    auth/         JWT mint/verify, bootstrap codes, JWKS provider
    broker/       Dispatch router + bookkeeping + circuit breaker + budget guard
    routes/       admin, a2a, tasks, well-known, auth, healthz
    observability/ Trace exporter + Langfuse adapter
  cli/            cbl entrypoint
  tui/            cabal entrypoint — the Textual terminal UI
  agents/         LLMPersona, EchoAgent, and personas/ (MasonPrimusPersona)
  archivist/      Shared-memory sync: gitmemory, dedup, promote, store
  librarian/      Document indexing + adapters
  handlers/       liminal-facing MCP handlers: archivist, code, documents, patterns
  codex/          Code-graph blame and repo resolution
  config/         identity.toml loader
  migrations/     Alembic revisions (ship as package data; cabal#79)
tests/            pytest suite
scripts/          bootstrap.sh, demo.sh
compose/          identity.dev.toml, litellm-config.yaml, litestream.yml
```

## Quickstart (development)

**Prerequisite**: clone `tenishai/techne` and `tenishai/liminal` as siblings:

```
parent/
├── cabal/        # this repo
├── liminal/      # github.com/tenishai/liminal
└── techne/       # github.com/tenishai/techne
```

> **Upgrading across the `limenal` → `liminal` rename**
>
> The gateway was previously misspelled `limenal`. If your checkout predates
> the rename:
>
> 1. `pip uninstall limenal` before reinstalling. The distribution *name*
>    changed, so pip treats the two as unrelated projects and leaves the old
>    one in place — a stale editable finder pointing at the deleted
>    `src/limenal/`, plus a `limenal` console script that now fails with an
>    import traceback rather than "command not found". Fresh CI runners are
>    unaffected, which is exactly why this bites only developer machines.
> 2. Rename the sibling directory to `liminal` — `docker-compose.yml` builds
>    it by path (`context: ../liminal`).
> 3. Rename `[limenal]` to `[liminal]` in your **untracked** `identity.toml`.
>    Nothing in the repo can do this for you, and the config model allows
>    unknown keys, so a stale section is absorbed silently: `[liminal].url`
>    falls back to `None` and every persona comes up with no tool surface.
>    Since this PR that miss at least logs `personas.mcp_skipped`.
> 4. `LIMENAL_*` environment variables are now `LIMINAL_*`. There is no
>    back-compat shim — a stale `.env` fails loudly on a missing variable
>    instead of silently reading a name nothing sets. Note that
>    `LIMINAL_SECRET_GUARD=0` set under the old spelling means the guard
>    comes back **on** until you rename it.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ../techne
pip install -e ../liminal

# Generate a master key
export CABAL_MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

cbl daemon up
# In another terminal: grab the bootstrap code from the log
cbl login --code <CODE>
cbl dispatch echo "hello cabal"
```

Run the unit suite (no sibling deps needed):

```bash
pytest -q
```

## Quickstart (Docker Compose)

```bash
make up                  # dev stack; nothing to prepare, see the note below
make bootstrap           # mints operator + liminal service token, restarts liminal
make demo                # 6-step smoke test: healthz → mint → init → tools/list → set → get
```

Logs: `make logs`. Tear down: `make down`.

`make up` runs the **dev** stack (`docker-compose.dev.yml`), whose identity file
is `compose/identity.dev.toml` — tracked, so a fresh clone already has it. Set
`CABAL_IDENTITY_FILE` to point somewhere else.

The **production** stack is different and is invoked directly, never through
`make`:

```bash
cp identity.toml.example identity.toml   # REQUIRED — docker-compose.yml bind-mounts
                                         # ./identity.toml, which is gitignored. Without
                                         # this, Docker creates an empty DIRECTORY at
                                         # that path and cabal-core fails reading it.
docker compose -f docker-compose.yml up
```

It adds a LiteLLM proxy and a Litestream sidecar replicating the SQLite DB.

> `CABAL_MASTER_KEY` encrypts the `signing_keys` and `secrets` tables.
> `Makefile:6` generates a fresh one — but only when `.env` is absent, and `.env`
> is currently **tracked in git**, so a fresh clone gets the committed key and the
> rule never fires. Rotating that key and untracking the file is #196; until it
> lands, generate your own with
> `python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`
> and replace the value before running anything you care about.

## Configuration (identity.toml)

```toml
[operator]
operator_id = "alice"

[personas.archivist]
role = "LLM"
model = "claude-haiku-4-5-20251001"
system_prompt = "You are an archivist."
budget_tokens = 20000

[personas.archivist.mcp]
enabled = true
max_steps = 6
allowed_handlers = ["memory", "scratchpad"]
tool_extras = { "memory.db_path" = "/data/archivist.db" }

[langfuse]                        # optional; omit to disable tracing
host = "https://cloud.langfuse.com"
public_key_env = "LANGFUSE_PUBLIC_KEY"
secret_key_env = "LANGFUSE_SECRET_KEY"
```

### `CABAL_DISPATCH_TIMEOUT_S`

Environment variable (not identity.toml). Bounds the read ceiling of the
HTTP client used for federated dispatch — default 30s, the historical
hardcoded value. Raise it if a federated peer fronts a real model call and
can legitimately take minutes; connect stays pinned at 10s regardless, so an
unreachable peer still fails fast rather than hanging for the full read
ceiling. Must parse as a finite number > 0.

Coupled to the doom-loop guardrail: `DoomLoopGuardrail`'s TTL (default 600s,
`guardrails.doom_loop_ttl_s`) has to stay above the dispatch timeout or a
hanging federated peer resets the guard's repeat count on every attempt
before it ever reaches its threshold. Rather than requiring operators to
hand-tune both, cabal auto-widens the TTL to `2 * CABAL_DISPATCH_TIMEOUT_S`
whenever the configured TTL would fall below that, logging
`guardrails.doom_loop_ttl_raised` when it does. In practice: raising this
above 300s (half the default TTL) triggers the widening automatically.

**`cbl` follows it automatically, on dispatch routes only.** The CLI's own HTTP
client used to be pinned at 30s, so raising this variable produced a daemon
that waited minutes and a `cbl` that gave up at 30s — the exact failure the
knob exists to prevent, on the route the knob is for. `cbl` now resolves the
same variable and allows `2 * CABAL_DISPATCH_TIMEOUT_S` for the two routes
that trigger a federated dispatch (`cbl dispatch` / `cbl chat`), since the client
additionally waits on auth, bookkeeping and the response trip. Most other
`cbl` calls keep the short 30s ceiling on purpose: a hung daemon should not
make `cbl agents list` sit for minutes. The session-presence calls
(`register_session`, `heartbeat_session`, `end_session`) are tighter still at
5s: they run from a Stop hook on every turn, so a firewall that DROPs rather
than refuses would stall the turn itself. An unparseable value fails the CLI
the same way it aborts the daemon, rather than silently falling back to 30.

## Recovering a lost master key

`CABAL_MASTER_KEY` encrypts the `signing_keys` and `secrets` tables. Start cabal
against a database written under a *different* key and it stops before binding
the port:

```
[error] cabal.master_key_mismatch  db_url=... key_source='the CABAL_MASTER_KEY environment variable'
        msg='Could not decrypt this database's signing key: either the master key is
             not the one that wrote it, or the stored row is damaged. ...'
```

Three ways out, cheapest first:

1. **Restore the original key** — if the key is what is wrong. Then nothing is
   lost: the data was never damaged, only unreadable. The message says *either
   the key or a damaged row* because cabal genuinely cannot always tell: a
   malformed ciphertext envelope is detected and reported separately as
   `cabal.encrypted_row_damaged`, but damage inside the encrypted body is
   indistinguishable from a wrong key without the key itself. If restoring the
   key does not work, assume the row is damaged and go to 2 or 3.
2. **Point somewhere else**: `CABAL_DB_URL=sqlite+aiosqlite:///data-new/cabal.db`
   leaves the old database untouched for a later recovery.
3. **`cbl reset-auth`** — backs the database up, deletes the undecryptable
   signing keys, and drops the cached operator token they signed. The next
   `cbl daemon up` mints a fresh key and prints a new bootstrap code.

`reset-auth` keeps your `secrets` rows by default. They stay encrypted under the
key you no longer have, so they are unreadable until it comes back — but they are
also the only thing here that cannot be regenerated, which is why deleting them
takes an explicit `--purge-secrets`.

If the daemon starts but dispatch fails with `No key for kid`, the saved token
was signed by a key that no longer exists: run `cbl login --code <CODE>` again.

## Seeing the fleet (`cbl status`)

`cbl status` lists the personas this cabal owns. It also lists **live agent
sessions** — the Claude Code sessions actually doing work — once they register:

```
NAME                    GEN KIND       ROLE       STATUS     TIER     HEARTBEAT
-------------------------------------------------------------------------------
echo                      1 member     builtin    active     strict   —
poe2                      1 session    session    active     strict   2026-09-10T12:04:00+00:00
                       ↳ liminal · fix/73-hardening · working the hardening bill
poe3                      1 session    session    active     strict   2026-09-10T11:31:00+00:00  STALE
```

A session row is **presence only** — it exists to answer "what is running, and
what is it doing". Nothing can route work to it, and the reason is not the empty
capability map on the row: capability routing matches against an in-memory
registry that only `identity.toml` personas are ever added to, so a session is
absent from it entirely. The `session://` endpoint and empty agent card are
belt to that suspenders.

Wire it up with the hook cabal ships:

```jsonc
// ~/.claude/settings.json, or a project .claude/settings.json
"hooks": {
  "SessionStart": [{"hooks": [{"type": "command",
     "command": "/path/to/cabal/hooks/cabal-session-presence.sh start"}]}],
  "Stop":         [{"hooks": [{"type": "command",
     "command": "/path/to/cabal/hooks/cabal-session-presence.sh beat"}]}]
}
```

Set `CABAL_SESSION_NAME=poe2` for a readable roster (it falls back to a short
session id), and `CABAL_SESSION_TASK="..."` for the one-line what-I-am-doing.
Repo and branch are read from git; a detached worktree reports `detached@<sha>`.

The hook **fails open everywhere** — no `cbl` on PATH, a daemon that is down, or
a failing call all exit 0. Presence is an operator convenience and must never
break a turn.

`STALE` marks a session that has not heartbeated in 15 minutes. Clean
de-registration cannot be relied on — a crashed session never runs its Stop
hook — so staleness, not `cbl session end`, is what makes a dead session read
as dead. Reaping those rows is tracked in #70.

## Modes

- **Solo (default)**: one process, all personas as local handlers, dispatch router
  shortcuts the network for in-process calls.
- **Scale**: per-agent containers, dispatch router routes via HTTP.

Same package, same code, same tests — `--agent` flag at startup decides which
handlers register locally.

## License

Copyright (C) 2026 tenishai

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>.

Full text: [LICENSE](LICENSE).
