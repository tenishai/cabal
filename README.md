# Cabal

cabal is a self-hosted daemon that runs a small team of LLM personas and routes work
between them and other agents over A2A. It is the identity provider (it mints and
verifies the JWTs), the dispatch broker that decides who may call whom and logs every
hop, and the encrypted store for the credentials your agents' tools need. Run it when
you have more than one agent and want one place on your own machine that connects them,
scopes them and keeps their keys.

For an agent: `cbl guide welcome` prints the bundled guide to what cabal is, what it can
do for you, and every option with why you'd want it. It ships in the package, so it
describes the installed version.

The distribution is named `tenish-cabal` (`pip show tenish-cabal`; extras install as
`tenish-cabal[mcp]`); the import package is still `cabal` and the commands are `cbl` and `cabal`.
`v0.3.0` is the first tag that builds `tenish-cabal`; `v0.2.0` builds `cabal`.

## Where this fits

cabal is the orchestrator. Each project below is optional to it.

- **liminal** (MCP tool gateway), extra `mcp`: personas call their tools through it, and
  it loads cabal's four handlers and fetches credentials from cabal.
- **techne** (memory library), extra `memory`: persona recall, and the corpora behind the
  documents, patterns and code handlers.
- **daedalus** (agent construction kit): no import either way. A daedalus agent runs as
  its own process, and cabal dispatches to it over A2A once you register it with
  `cbl persona graduate`.
- **kobo** ("the room"): no import. It mints `colleague` tokens cabal accepts and syncs
  `PUT /admin/dispatch-pairs`.
- **gojira**: `gojira.yaml` deploys cabal to GKE.

**Where an agent is defined.** A cabal persona is a `[personas.<name>]` block in
`identity.toml`, and the name must be one cabal has code for (`architect`, `archivist`,
`librarian`, `mason`, `wright`; `PERSONA_REGISTRY` in `agents/personas/__init__.py`).
Any other name gets a bulletin-board row and no handler. A daedalus agent is defined in
its own bundle directory and run by daedalus; cabal only holds a federated row pointing at
its endpoint, and sends it Tasks at `POST <endpoint>/a2a/tasks`.

**The handlers ship unconfigured.** The archivist, documents, patterns and code handlers
run inside liminal and have no corpus until you name one: `CABAL_ARCHIVIST_DB` turns on
shared team memory, `CABAL_LIBRARIAN_DB` document search, and `CABAL_ARCHITECT_DB` pattern
search and recording. `code` reads no env var and needs a persona's `code.db_path`.
Details in [Handler status in the shipped stacks](#handler-status-in-the-shipped-stacks).

## What works

- **Full A2A dispatch loop** — submit a task via HTTP or CLI, get a completed
  Task back. In-process local dispatch and HTTP remote dispatch both work (the dispatch and
  router tests pass as of 2026-11-23; no second live daemon was run).
- **JWT trust boundary** — every token is verified as RS256 only, at every
  verify site, so an HS256 token with a real `kid` fails. Every `/admin` route
  checks audience and scope, and every dispatch checks the caller token's
  audience. Some paths check less. `/healthz`, the agent cards,
  `/.well-known/jwks.json` and `/auth/bootstrap` (which takes a one-time code)
  take no token. A2A v1.0 GetTask, ListTasks and CancelTask and the legacy
  `tasks/get` check that the caller owns the task (or holds `operator` scope),
  not the token's audience, so any validly signed, unrevoked token can call
  them (issue #296). A token whose `aud` already matches the dispatch target is passed on as
  is, with no scope check. Operator tokens cannot impersonate liminal service
  tokens and vice versa. `allow_dev_master_key` defaults to `False`.
- **liminal integration** — personas that have `[mcp]` wired in their identity.toml
  persona block get a `tool_proxy` that dispatches to liminal over JSON-RPC. The
  credential flow (`cbl secret set` → `/admin/secrets` → `ctx.credentials` in the
  handler) is tested end to end with liminal and cabal in one process
  (`tests/test_credential_flow_e2e.py`, reported passing 2026-11-23 against liminal 0.7.1; it skips when liminal isn't installed).
- **LLM personas** — `LLMPersona` wraps LiteLLM acompletion with tool-use loop,
  budget guard, circuit breaker, and per-step hook callbacks.
- **CLI** — `cbl` covers daemon, login, dispatch (also detached, with `cbl task`),
  chat, tail, `status`, `tasks`, `drain`, `review`, `conversations`,
  `token renew` and `token mint-service`, `secret set|unset|list`,
  `reload identity`, `persona graduate|absorb`, `primus rotate`, `session`,
  `reset-auth`, `archivist purge|erase|backfill-origin`, and `cbl guide`.
- **Review runs and a merge gate** — `cbl review start|list|show|cancel` queue and
  follow one review per pull-request head, taken through its seats in order (default
  `temperance`, which checks the assertions a change adds, then `prudence`, which
  reviews correctness). Off unless `[review] enabled`. A watch loop queues a
  run for each PR that carries the `ready-for-review` label in a repo listed under
  `[review.repos."owner/name"]`, and a repo that sets `merge = true` has approved runs
  merged, pinned to the reviewed commit, once CI is green. `identity.toml.example`
  describes every key; the CHANGELOG lists the checks the gate makes.
- **Group dispatch and answers** — `[groups]` in identity.toml names agents in order;
  `cbl dispatch --detach group:<name> "<brief>"` starts the task on the first member with
  nothing running. A task that stops in `input-required`, group or not, takes
  `cbl task <id> --answer "<text>"`, which puts it back in the queue.
- **Token budgets** — a primus's fan-out has a ceiling and each worker an allotment
  (`leg_token_budget_override`, `fanout_token_budget_override`). For federated agents,
  `[meter]` passes their model calls through `/meter/<key>/...` and charges the
  tokens to the task they run; `task_budget` and `root_budget` refuse calls once the task has spent its budget.
- **Docker Compose dev stack** — `make up / bootstrap / demo` runs the full
  cabal + liminal stack; `make demo` is a 6-step smoke test. It needs the running stack, so the
  release pass did not measure how often it passes.
- **A2A v1.0 interface** — `POST /agents/{name}/a2a` (with `A2A-Version: 1.0`)
  serves SendMessage, GetTask, ListTasks and CancelTask, including
  `returnImmediately` and conversations by `contextId`. Each persona has a signed
  v1.0 card at `/agents/{name}/.well-known/agent-card.json` once `public_url` is
  set. The legacy routes (`/agents/{name}`, its `/stream`, `/a2a/tasks`) still
  serve clients that send no `A2A-Version`; a request that sends one gets `-32009`
  (HTTP 400 on `/a2a/tasks`).
  `docs/outside-agents.md` says how to admit an outside agent.
- **Colleagues and dispatch pairs** — the room (kobo) mints colleague tokens and
  syncs who may call whom with `PUT /admin/dispatch-pairs`. An empty table means
  open, and emptying it logs `dispatch_pairs.replaced_empty`. `GET /directory`
  lists only the agents a colleague may dispatch to, with each one's
  `agent_card_url` (null until `public_url` is set).
- **Refusals are logged, audited and actionable** — every refusal of a dispatch
  token (bad token, revoked, wrong audience, missing scope, missing colleague pair)
  logs `dispatch.refused` (never the token), and one whose token verified also
  writes a `dispatch_refused` event row. Other refusals (a caller JWT the router
  rejects, a draining daemon, a guardrail block, a refused scheduled start) do
  not log it. A refusal for a missing colleague pair carries
  `"remedy": "colleague_invite"` and says which owner invites and which accepts.
- **Token revocation** — `POST /admin/token/revoke` (operator) refuses every token
  a subject was issued before the cutoff, and the cutoff survives a restart. An
  `aud=liminal` token already exchanged is checked by the gateway, not here, and
  lives out its 15 minutes. The gateway can fetch every cutoff from
  `GET /auth/revocations`, using the same token it uses to fetch secrets.
- **Bootstrap code delivery** — `cabal.bootstrap_code` (`log`, `file` or `off`)
  lets a cabal that already has its operator keep the startup code out of the log.

## Layout

```
src/cabal/
  core/           FastAPI + SQLite + A2A broker + bulletin board
    auth/         JWT mint/verify, bootstrap codes, JWKS provider
    broker/       Dispatch router, bookkeeping, heartbeat, waiting
    routes/       admin, a2a_v1, a2a_jsonrpc, tasks, well_known, auth, healthz,
                  directory, meter, reviews
    observability/ Trace exporter + Langfuse adapter
  cli/            cbl entrypoint
  tui/            cabal entrypoint — the Textual terminal UI
  agents/         LLMPersona, EchoAgent, and personas/ (architect, archivist,
                  librarian, mason, wright)
  archivist/      Shared-memory sync: gitmemory, gitsync, dedup, promote, store,
                  schema, models; purge, erase, backfill (admin-only, CLI)
  guides/         Markdown guides bundled in the wheel (`cbl guide`)
  librarian/      Document indexing + adapters
  handlers/       liminal-facing MCP handlers: archivist, code, documents, patterns
  codex/          Code-graph blame and repo resolution
  config/         identity.toml loader
  migrations/     Alembic revisions (ship as package data; cabal#79)
tests/            pytest suite
scripts/          bootstrap.sh, demo.sh, test-docker.sh, check-techne-sibling.sh,
                  check_arch_cards.py, a2a_tck/
compose/          identity.dev.toml, litellm-config.yaml, litestream.yml
```

## Handler status in the shipped stacks

The `handlers/` modules load inside liminal (`liminal serve`), and no shipped
compose or k8s file sets their corpus paths, so they are not configured out of
the box (tenishai/cabal#129 tracks the wiring). Each needs a corpus db that you
point at yourself, and `documents`, `patterns` and `code` also need `techne`
importable (`pip install tenish-cabal[memory]`):

- **`archivist`**: set `CABAL_ARCHIVIST_DB` on the liminal process, or give a
  persona `archivist.db_path` in its `tool_extras`. Without either, every call
  fails with "no archivist corpus configured".
- **`documents`** (librarian): set `CABAL_LIBRARIAN_DB`, or a persona
  `documents.db_path`. Without either, every call fails with "no librarian
  corpus configured". `documents.refresh` is operator-only (see below).
- **`patterns`**: set `CABAL_ARCHITECT_DB`, or a persona `patterns.db_path`.
  `patterns.record` is operator-only (see below).
- **`code`**: has no env var. The only path is a persona `code.db_path` in
  `tool_extras`, and the handler has no repo list, so `code.refresh` has
  nothing to index and rejects any named repo as unknown. The codex is a db per repo, which one path cannot address;
  the design is still open in #129.

With liminal 0.6.0 or later, `/healthz` shows each of these handlers as
`unconfigured`, with the reason, until its default corpus is set (the code
handler always shows it, since it reads no env var). Older liminal ignores
the handlers' `status()` and shows them `ready`. The tools stay listed either
way, because a persona's `tool_extras` can still name a corpus.

## Quickstart (development)

**Prerequisites**: Python 3.12 or newer, `git`, and on Debian/Ubuntu
`sudo apt update && sudo apt install python3-venv` (a stock install lacks the
`venv` module).
cabal, techne and liminal are cloned as siblings:

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
>    That miss now logs `personas.mcp_skipped`.
> 4. `LIMENAL_*` environment variables are now `LIMINAL_*`. There is no
>    back-compat shim — a stale `.env` fails loudly on a missing variable
>    instead of silently reading a name nothing sets. Note that
>    `LIMINAL_SECRET_GUARD=0` set under the old spelling means the guard
>    comes back **on** until you rename it.

```bash
mkdir parent && cd parent
git clone https://github.com/tenishai/cabal.git
git clone --branch v0.18.2 https://github.com/tenishai/techne.git   # the tag the Dockerfile pins
git clone https://github.com/tenishai/liminal.git
cd cabal

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ../techne
pip install -e ../liminal

# Generate a master key
export CABAL_MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# Name the operator. Without this (or `operator_id` under `[cabal]` in
# identity.toml), `cbl login` fails with "operator_id not configured".
# The daemon reads it, so set it in the shell that starts the daemon.
export CABAL_OPERATOR_ID=alice

cbl daemon up
# In another terminal: grab the bootstrap code from the daemon's log
cbl login --code <CODE>
cbl dispatch echo "hello cabal"
```

The operator token lasts an hour (`[cabal] operator_token_ttl_seconds`). `cbl` renews it by
itself when it has under ten minutes left, and `cbl token renew` renews it now. One that has
already expired needs a new bootstrap code, which means a daemon restart.

Run the unit suite (no sibling deps needed; measured 2026-11-23 at commit 002ed91 on Python 3.12.15 with
`pip install -e ".[dev]"` only: 1826 passed, 20 skipped in about 4.5 minutes):

```bash
pytest -q
```

## Quickstart (Docker Compose)

```bash
make up                  # dev stack; needs the prerequisites below
make bootstrap           # within 5 minutes of `make up`: mints tokens, restarts liminal
make demo                # 6-step smoke test: healthz → mint → init → tools/list → set → get
```

Run `make bootstrap` within 5 minutes of `make up`: the bootstrap code cabal prints at
startup expires after that. If it has, restart cabal for a fresh code
(`docker compose -f docker-compose.dev.yml restart cabal`) and run it again.
`make bootstrap` waits for liminal to come back healthy before it returns, so
`make demo` can follow straight away.
`make bootstrap` reads the code from cabal's log, so it needs the default
`bootstrap_code = "log"`; `identity.toml.example` describes `file` and `off`.

**Prerequisites for `make up`**: Docker with the Compose plugin, and `docker buildx`
(on Ubuntu: `sudo apt update && sudo apt install docker-buildx`). The dev compose file builds the cabal
and liminal images from sibling checkouts of `techne` and `liminal` (BuildKit named
build contexts, which need buildx), so clone both next to this repo as in the
layout above before running it. `make build` and `make up` first check that
`../techne` is at the tag the Dockerfile pins (`TECHNE_VERSION`, currently
`v0.18.2`) and stop if it isn't; set `TECHNE_ANY=1` to build against whatever
`../techne` has checked out, for example when changing techne and cabal
together. The generated `.env` and the tracked
`compose/identity.dev.toml` cover the rest, so there is nothing else to prepare.

Logs: `make logs`. Tear down: `make down`.

`make up` runs the **dev** stack (`docker-compose.dev.yml`), whose identity file
is `compose/identity.dev.toml` — tracked, so a fresh clone already has it. Set
`CABAL_IDENTITY_FILE` to point somewhere else.

The **production** stack (`docker-compose.yml`) adds a LiteLLM proxy and a
Litestream sidecar replicating the SQLite DB. It is invoked directly, never
through `make`, and **it is not a two-line quickstart** — it needs four things
the dev stack does not:

1. `cp identity.toml.example identity.toml`. The compose file bind-mounts
   `./identity.toml`, which is gitignored; without it Docker creates an empty
   *directory* at that path and cabal-core fails reading it.
2. `CABAL_MASTER_KEY` in `.env`, also `:?`. `.env` is gitignored and only
   `make` creates it, so a production checkout has to write it:
   `echo "CABAL_MASTER_KEY=$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")" >> .env`
3. `LITELLM_MASTER_KEY` in `.env`. Declared `:?`, so compose refuses to start
   anything without it.
4. `LIMINAL_CABAL_TOKEN` in `.env`, also `:?`. This one is circular: the token
   is minted by cabal-core, and compose checks every `:?` variable in the file
   before starting any service, including when you start only cabal-core. The
   dev stack avoids it (the token is optional there, and `make bootstrap` fills
   it in). For production, start from a placeholder:

   ```bash
   echo 'LIMINAL_CABAL_TOKEN=placeholder' >> .env
   docker compose -f docker-compose.yml up -d cabal-core
   docker compose -f docker-compose.yml logs cabal-core | grep bootstrap_code
   cbl login --code <CODE>                         # the code from that log line
   cbl token mint-service --scope secrets-fetch    # prints the token
   # replace the placeholder in .env with the printed token, then:
   docker compose -f docker-compose.yml up -d
   ```

`docker compose -f docker-compose.yml config` exits non-zero until 2, 3 and 4
are set — run it first to check rather than discovering it during `up`.

Because this stack is not built through `make`, nothing runs the techne check
for you: run `bash scripts/check-techne-sibling.sh` before the first `up`. The
image build itself only compares version strings, which cannot tell techne's
main from the tag.

> `CABAL_MASTER_KEY` encrypts the `signing_keys` and `secrets` tables. For the
> dev stack, `make up` writes a fresh one into `.env` on first run (`Makefile:8`),
> and `.env` is gitignored. Keep that key: change it and the stored rows can no
> longer be decrypted (`.env.example` explains `cbl reset-auth`). A key that was
> committed to this repo before 2026-09-23 is rotated and dead, but it is still in
> git history (#196), so never reuse anything from an old `.env`.

## Configuration (identity.toml)

```toml
[cabal]
operator_id = "alice"

[personas.archivist]
description = "Memory and search"
role = "singleton"                # "singleton" or "primus"
safety_tier = "moderate"
model_override = "claude-haiku-4-5-20251001"   # optional
system_prompt_override = "You are an archivist."  # optional

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

### Where cabal keeps its state

Since 0.3.0, cabal reads `--config` if you pass it. Otherwise it picks a state
root and reads `identity.toml` from there. The state root is the working
directory if it already holds `identity.toml`, `data/cabal.db` or
`data/.master_key`, so older installs keep working in place; otherwise it is
`$CABAL_HOME`, which defaults to `~/.cabal`. The database and the dev master key
live under the state root, and `CABAL_DB_URL` overrides the database path. The
CLI token is always `$CABAL_HOME/token`.

### Settings added in 0.3.x

Each `[cabal]` setting below is described in `identity.toml.example`. Where an
environment variable is listed, it wins over the file.

| Setting | Env var | Default | What it does |
|---|---|---|---|
| `data_dir` | `CABAL_DATA_DIR` (new in 0.3) | `"data"` | Anchors relative `[techne]` paths such as `db_path`. It does not set the database path (see `CABAL_DB_URL`). The image sets the env var to `/data`. |
| `public_url` | `CABAL_PUBLIC_URL` | unset | The URL clients reach cabal at. A2A v1.0 cards answer 503 without it. |
| `front_door` | `CABAL_FRONT_DOOR` | unset | The persona whose v1.0 card `/.well-known/agent-card.json` serves; 404 when unset. |
| `context_replay_messages` | | `40` | How many earlier messages a v1.0 SendMessage with a `contextId` replays; 0 for none. |
| `detached_task_limit` | | `16` | Most `returnImmediately` tasks running at once; more get `-32004`. |
| `detached_drain_seconds` | | `10` | How long each detached task gets to stop at shutdown before it is cancelled. |
| `bootstrap_code` | `CABAL_BOOTSTRAP_CODE` | `"log"` | `file` writes the code to `$CABAL_HOME/bootstrap_code` (mode 0600) and logs only the path; `off` mints none. An unknown value stops the start. |

`[guardrails] caller_edges` says which personas an `agent:<name>` caller may reach
when `delegation_edges = true`; see `docs/outside-agents.md`.

### `CABAL_DISPATCH_TIMEOUT_S`

Environment variable (not identity.toml). Bounds the read ceiling of the
HTTP client used for federated dispatch — default 30s, the historical
hardcoded value. Raise it if a federated peer fronts a real model call and
can legitimately take minutes; connect stays at 10s (or the whole timeout, if that is shorter), so an
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
make `cbl status` sit for minutes. The session-presence calls
(`register_session`, `heartbeat_session`, `end_session`) are tighter still at
5s: they run from a Stop hook on every turn, so a firewall that DROPs rather
than refuses would stall the turn itself. An unparseable value fails the CLI
the same way it aborts the daemon, rather than silently falling back to 30.

**Detached tasks.** `cbl dispatch --detach <agent> "text"` returns at once with the task id
(A2A `returnImmediately`, issue #277); `cbl task <id>` reads it and `cbl task <id> --wait
[--interval S] [--timeout S]` polls until it ends. The daemon runs the task, so a client
restart or network blip does not lose it, and `cbl`'s own ceiling does not apply. The
daemon's does: a detached task's call to a federated peer goes through the same HTTP client
as a blocking dispatch, so `CABAL_DISPATCH_TIMEOUT_S` still bounds it, and a peer that takes
longer fails the task. Raise the variable on the daemon for long peer tasks; the doom-loop
TTL widening above applies unchanged. A local persona has no such ceiling. `cbl task`
exits 1 for a task that did not complete and 3 when `--timeout` runs out first.
`cbl task` finds the persona through the operator's conversation view; with a persona token,
pass `--target <agent>`.

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

## Removing an archivist entry (`cbl archivist purge`)

To stop serving an entry that is no longer true, publish a tombstone that
supersedes it through `archivist.publish` (`supersede: true`; add
`supersede_scope: "resource"` to retire the name's entries of every type).
The old row stays in the table for audit, and search stops returning it.

When the text must not be stored at all (personal data, a leaked secret), purge
it on the host that holds the database. For the archivist container, that's
`docker exec -it archivist cbl archivist purge <resource_name>`:

```sh
cbl archivist purge <resource_name>            # dry run: lists every version
cbl archivist purge <resource_name> --apply    # deletes
```

The name must match exactly. `--db` names the database file and defaults to
`$CABAL_ARCHIVIST_DB`. `--apply` deletes every version of the entry, whatever
its status or type, together with its full-text index rows and every relation
that touches it. It then runs the FTS5 `optimize` and `VACUUM`, so the text
leaves the index and the file. If `--apply` fails after the delete, re-running
it finishes that cleanup. The output and the log carry ids, types, statuses and
content hashes, never the entry's text.

No MCP tool or HTTP route can reach this command. A writer that still holds the
fact (for example a techne row tagged `team`) will publish it again, so remove
it there first. If the corpus is mirrored with gitsync, also delete
`entries/<content_hash>.json` from the memory repo; git history keeps it until
that history is rewritten.

## Seeing the fleet (`cbl status`)

`cbl status` lists the personas this cabal owns. It also lists **live agent
sessions** — the Claude Code sessions actually doing work — once they register:

```
NAME                    GEN KIND       ROLE       STATUS     TIER     HEARTBEAT
-------------------------------------------------------------------------------
echo                      1 member     builtin    active     strict   —
agent-b                   1 session    session    active     strict   2026-09-10T12:04:00+00:00
                       ↳ liminal · fix/73-hardening · working the hardening bill
agent-c                   1 session    session    active     strict   2026-09-10T11:31:00+00:00  STALE
```

A session row is **presence only** — it exists to answer "what is running, and
what is it doing". Capability routing cannot select it, and the reason is not the
empty capability map on the row: capability routing matches against an in-memory
registry that only `identity.toml` personas are ever added to, so a session is
absent from it entirely. Naming a session as an explicit dispatch target is not
refused by kind; its `session://` endpoint has no transport, so the dispatch
fails there. The `session://` endpoint and empty agent card are belt to that
suspenders.

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

Set `CABAL_SESSION_NAME=agent-b` for a readable roster (it falls back to a short
session id), and `CABAL_SESSION_TASK="..."` for the one-line what-I-am-doing.
Repo and branch are read from git; a detached worktree reports `detached@<sha>`.

The hook **fails open everywhere** — no `cbl` on PATH, a daemon that is down, or
a failing call all exit 0. Presence is an operator convenience and must never
break a turn.

`STALE` marks a session that has not heartbeated in 15 minutes. Clean
de-registration cannot be relied on — a crashed session never runs its Stop
hook — so staleness, not `cbl session end`, is what makes a dead session read
as dead. Reaping those rows is tracked in #70.

## Operator-only tools (`documents.refresh`, `patterns.record`)

liminal refuses these two tools unless the caller's token holds `operator`
scope alongside `dispatch`. Persona tokens carry only `dispatch` and the
`cbl login` token carries only `operator`. `cbl token mint-service` answers 400
to any request for `operator` scope or an `operator` subject (#403), and no
other cabal route mints a token holding both, so these two tools cannot
currently be reached with cabal-minted tokens (issue #453). In a sovereign liminal the policy grant must hold
`operator` instead, and with `LIMINAL_AUTH_DISABLED=1` these tools are refused
outright. This needs a liminal release that has `requires_operator`.

## Modes

- **Solo (default)**: one process, all personas as local handlers, dispatch router
  shortcuts the network for in-process calls.
- **Scale**: some personas run elsewhere and the router reaches them over HTTP. Today
  that is federation: `cbl persona graduate` points a persona at a remote endpoint.

`cbl daemon up --agent <name>` is accepted and logged, but nothing reads it yet: every
daemon registers every persona its `identity.toml` declares.

## How this was built

tenishai is a small team of engineers who build with AI coding agents. Most of
this code was written in pairing sessions with Claude models in Claude Code: a
person set the design, directed the work and reviewed what landed, and the
agent wrote much of the code and docs. Many commits name the agent in a
`Co-Authored-By:` trailer, either a Claude model or the name of the
agent one of us pairs with. A commit without a trailer may still
have had AI help.

## License

Copyright (c) 2026 tenish and the cabal contributors

MIT. See [LICENSE](LICENSE).
