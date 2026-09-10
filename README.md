# Cabal

Self-hosted personal agent orchestration platform. Cabal Core is the SQLite-backed
control plane and A2A broker; personas (Architect, Archivist, Librarian, ...) and
the Mason primus run as agent modules registered with the bulletin board.

## Status

**305 tests passing.** All phases through the security + hygiene sweep are complete:
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
  personas/       LLMPersona, MasonPrimusPersona, EchoPersona
  config/         identity.toml loader
migrations/       Alembic revisions
tests/            pytest suite (305 tests)
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
make up
make bootstrap           # mints operator + liminal service token, restarts liminal
make demo                # 6-step smoke test: healthz → mint → init → tools/list → set → get
```

Logs: `make logs`. Tear down: `make down`.

The dev stack (`docker-compose.dev.yml`) includes cabal + liminal only. The
production stack (`docker-compose.yml`) adds a LiteLLM proxy and Litestream
sidecar replicating the SQLite DB.

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
echo                      1 member     builtin    active     strict   —
poe2                      1 session    session    active     strict   2026-09-10T12:04:11Z
                            ↳ liminal · fix/73-hardening · working the hardening bill
poe3                      1 session    session    active     strict   2026-09-10T11:31:02Z  STALE
```

A session row is **presence only**. cabal gives it a `session://` endpoint and
an empty capability map, so nothing can route work to it — it exists to answer
"what is running, and what is it doing".

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

AGPL-3.0-or-later. See [LICENSE](LICENSE).
