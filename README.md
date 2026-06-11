# Cabal

Self-hosted personal agent orchestration platform. Cabal Core is the SQLite-backed
control plane and A2A broker; personas (Architect, Archivist, Librarian, ...) and
the Rook primus run as agent modules registered with the bulletin board.

## Status

Pre-alpha. Phase 1 (Cabal Core skeleton + first end-to-end dispatch loop) in
progress. See `../claude/cabal-handoff.md` for the build spec.

## Layout

```
src/cabal/        Python package
  core/           FastAPI + SQLite + A2A broker + bulletin board
  cli/            cbl entrypoint
  agents/         Local handler interface (registered at startup)
  config/         identity.toml loader
migrations/       Alembic revisions
tests/            pytest suite (router tests run in both modes)
```

## Quickstart (development)

**Prerequisite**: clone `dimartin/techne` as a sibling directory of `cabal`:

```
parent/
├── cabal/        # this repo
└── techne/       # github.com/dimartin/techne
```

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ../techne                 # sibling checkout
cp identity.toml.example identity.toml

# Generate a master key once
export CABAL_MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Terminal 1: start the daemon
cbl daemon up

# Terminal 2: log in with the bootstrap code printed in the daemon log,
# then dispatch a task to the built-in echo agent.
cbl login --code <CODE>
cbl dispatch echo "hello cabal"
cbl status                       # see registered agents
```

## Quickstart (docker compose)

```bash
cp .env.example .env             # fill in CABAL_MASTER_KEY, LITELLM_MASTER_KEY, ANTHROPIC_API_KEY
cp identity.toml.example identity.toml
docker compose build
docker compose up
docker compose logs cabal-core | grep bootstrap_code   # grab the code
cbl login --code <CODE>          # from host (port 7400 published to 127.0.0.1)
cbl dispatch archivist "what do you remember?"
```

The compose stack includes a LiteLLM proxy (model list in
`compose/litellm-config.yaml`) and a Litestream sidecar (config in
`compose/litestream.yml`) replicating the SQLite DB to a local volume by
default.

Run the test suite with `pytest -q`.

## Modes

- **Solo (default)**: one process, all personas as local handlers, dispatch router
  shortcuts the network for in-process calls.
- **Scale**: per-agent containers, dispatch router routes via HTTP.

Same package, same code, same tests — `--agent` flag at startup decides which
handlers register locally. See `cabal-handoff.md` §6.12 for the dispatch router
invariants that hold in both modes.

## License

MIT. See [LICENSE](LICENSE).
