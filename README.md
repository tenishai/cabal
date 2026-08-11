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

**Prerequisite**: clone `dimartin/techne` and `dimartin/liminal` as siblings:

```
parent/
├── cabal/        # this repo
├── liminal/      # github.com/dimartin/liminal
└── techne/       # github.com/dimartin/techne
```

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

## Modes

- **Solo (default)**: one process, all personas as local handlers, dispatch router
  shortcuts the network for in-process calls.
- **Scale**: per-agent containers, dispatch router routes via HTTP.

Same package, same code, same tests — `--agent` flag at startup decides which
handlers register locally.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
