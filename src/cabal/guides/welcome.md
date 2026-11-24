# Welcome to cabal

*For an agent explaining cabal to its human. Print it with `cbl guide welcome`; it
describes the version that is installed, not the repo's main branch.*

## What it is

cabal is a self-hosted daemon that runs a small team of LLM personas on your machine and
routes work between them and other agents over A2A, with its own JWT identity provider
and an encrypted credential store. You'd run it when you want several agents (yours, and
peers like daedalus agents) to call each other through one place that decides who may
call whom, logs every hop, and keeps the keys.

## Where it fits

cabal is the orchestrator. It is not the tool gateway, the memory store or the agent
builder; those are separate projects, and every one of them is optional to cabal.

- **liminal** (MCP tool gateway), optional, extra `mcp`. cabal personas call their tools
  through liminal, and liminal loads cabal's four handlers (archivist, documents, patterns,
  code) by entry point and fetches credentials from cabal's `/admin/secrets`.
- **techne** (memory library), optional, extra `memory`. cabal imports it to give
  personas recall and to back the documents, patterns and code handlers.
- **daedalus** (agent construction kit), optional, no import either way. A daedalus agent
  is a separate process. cabal dispatches to it over A2A once you register it as a
  federated persona (`cbl persona graduate`).
- **kobo** ("the room"), optional, no import. It mints `colleague` tokens cabal accepts and
  pushes the who-may-call-whom table to `PUT /admin/dispatch-pairs`.
- **gojira** (deploy platform), optional. `gojira.yaml` in the repo deploys cabal to GKE.

## Where an agent is defined

There are two kinds, and they live in different places.

- **A cabal persona** is a `[personas.<name>]` block in `identity.toml`. The name must be
  one cabal has code for: `architect`, `archivist`, `librarian`, `mason` or `wright`
  (`PERSONA_REGISTRY` in `cabal/agents/personas/__init__.py`). Any other name gets a row
  on the bulletin board and no handler, and the daemon logs
  `personas.no_handler_registered`. A new kind of persona is a code change, not a config
  change.
- **A daedalus agent** is a directory (agent card, persona, `agent.yaml`) that daedalus
  runs on its own. cabal does not load it. Register it with
  `cbl persona graduate <name> --endpoint <url> --audience <id>`, and cabal's router sends
  it Tasks over HTTP at `POST <endpoint>/a2a/tasks`.

## What it can do for you

| Job | How |
|---|---|
| Run the daemon | `cbl daemon up` (binds 127.0.0.1:7400) |
| Log in as the operator | `cbl login --code <CODE>`, with the code from the daemon's startup log |
| Send one task to a persona | `cbl dispatch <agent> "text"` |
| Send a long task without holding the request open | `cbl dispatch --detach <agent> "text"`, then `cbl task <id> --wait` |
| Send work that starts later | `cbl dispatch --detach --not-before 17:00 <agent> "text"` (HH:MM is this machine's time zone, or give an ISO 8601 time); `cbl tasks --scheduled` lists what's waiting, `cbl task <id> --cancel` cancels one |
| Send work to whichever member of a group is free | `[groups]` in identity.toml names the members in order; `cbl dispatch --detach group:<name> "text"` starts it on the first member with nothing running, and it waits as `scheduled` while all are busy |
| Answer a task that stopped with questions | `cbl task <id> --answer "text"` (state `input-required`) puts it back in the queue; a group task goes to the next idle member, any other to the agent that asked |
| Have a pull request reviewed, and merged when approved | `cbl review start <owner/repo> <pr> --sha <sha>`, or label the PR `ready-for-review` in a repo under `[review.repos]`; `cbl review list`, `show`, `cancel` follow it. Needs `[review] enabled = true`; `merge = true` on a repo adds the merge gate |
| Talk to a persona | `cbl chat <agent>`, or the terminal UI `cabal` (boots its own in-process app) |
| See who is registered | `cbl status`, which also shows live Claude Code sessions that run the presence hook |
| Watch events as they happen | `cbl tail` |
| Read past conversations | `cbl conversations list`, `cbl conversations show <id>` |
| See what's running before a restart | `cbl tasks --running`; `cbl drain --wait` stops new work and waits for it to finish, `cbl drain --cancel` takes work again |
| Store a credential for a liminal handler | `cbl secret set <persona> <handler> <KEY>`; liminal fetches it |
| Mint a token for a service or another agent | `cbl token mint-service ...` |
| Pick up `identity.toml` edits without a restart | `cbl reload identity` |
| Hand a persona to a remote peer, or take it back | `cbl persona graduate`, `cbl persona absorb` |
| Recover from a lost master key | `cbl reset-auth` |
| Delete an archivist entry for good | `cbl archivist purge <resource_name>` (dry run), then `--apply` |
| Serve agents outside cabal | A2A v1.0 at `POST /agents/{name}/a2a`; signed cards once `public_url` is set |

## Options, and why you'd want each

### Solo vs Scale mode

**Solo** is what you get: one `cbl daemon up` process holds every persona, and the router
calls an in-process persona directly instead of over HTTP. Reach for it on one machine.

**Scale** means some personas run in other processes or on other hosts. The way to do that
today is federation: `cbl persona graduate` points a persona at a remote endpoint, and from
then on the router sends it Tasks over HTTP. `cbl daemon up --agent <name>` exists and is
logged at startup, but nothing reads it yet; every daemon still registers every persona its
`identity.toml` declares. Cost: a federated persona gives up streaming and resume.

### Extras

`pip install "tenish-cabal[<extra>]"`. Core alone runs the daemon, `cbl` and the echo agent.

| Extra | Pulls | Reach for it when |
|---|---|---|
| (none) | FastAPI, SQLAlchemy, Typer, litellm, ... | Always; the daemon, `cbl` and the personas need nothing else. |
| `tui` | `textual` | You want the `cabal` terminal UI. Without it `cabal` cannot start. |
| `mcp` | `tenish-liminal` | Any persona has `[personas.X.mcp] enabled = true`, or `[liminal] url` is set. A URL without liminal installed stops the daemon at startup. |
| `memory` | `tenish-techne` | You set `[techne] db_path`, or you serve the documents, patterns or code handlers. |
| `full` | liminal + techne | The whole platform. It does not include `tui`; add it: `[full,tui]`. |
| `dev` | pytest, ruff, mypy, pyyaml | You are changing cabal itself. |

### Personas and `safety_tier`

Each persona block takes `role`, `description`, `model_override`, `system_prompt_override`
and `max_tokens_override`, plus these, each with a reason to set it:

- `capabilities`: tags such as `["code-search"]`. A task sent with `required_capabilities`
  goes to a persona whose tags cover them, so a caller can ask for a skill without naming a
  persona.
- `may_delegate_to`: the personas this one may hand work to. It does nothing unless
  `[guardrails] delegation_edges = true`, and then **omitting it means no delegation**, the
  opposite of `mcp.allowed_handlers`, where omitting means every handler. Once delegation
  edges are on, set it on every persona that dispatches to another persona, or that dispatch
  is refused. A primus's fan-out to its own subordinates isn't gated by edges.
- `leg_token_budget_override` and `fanout_token_budget_override` (primus personas): the
  tokens each worker, the reviewer included, may spend, and the ceiling for the whole
  fan-out. Unset, a worker gets 200,000 and the fan-out one default budget plus one
  allotment per worker. A fan-out that cannot cover every worker and one aggregate call is
  refused before any worker starts.
- `commit_to_memory`: write the persona's own Q&A back into its memory store. Off by
  default for every persona; turn it on only for a persona whose store nobody else reads.
- `mcp`: liminal's tools for this persona (see "The liminal integration" below).
- `checkpointing`: `enabled = true` writes a checkpoint row on every model call, so a task
  that dies with the daemon can be resumed (`POST /admin/tasks/{id}/resume`). Reach for it
  on personas with long tool-use chains. Cost: a database write per step.

Models route through LiteLLM, set by `[litellm]` and `model_override`. Every setting is
commented in `identity.toml.example` in the repo.

`safety_tier` (default `"strict"`) is recorded on the persona's bulletin row and agent card
and shown by `cbl status`. cabal enforces nothing based on it today, so treat it as a label
other agents can read, not as a control. The controls are the guardrails (`[guardrails]`:
doom-loop, delegation depth, budget, `delegation_edges`) and `mcp.allowed_handlers`.

### Token meter, for federated agents

A federated agent makes its model calls in its own process. With `[meter] enabled = true`
and an entry under `[meter.agents.<name>]` (`key_sha256`, `upstream`), the agent's model base
URL is `<cabal>/meter/<key>`: cabal forwards each call to the upstream unchanged (it never
stores or swaps a credential), reads the token usage and charges input plus output tokens to
the task the agent is running. With no `task_budget` it only measures; `task_budget` and
`root_budget` refuse calls with a 400 once the task has spent its budget. Spend is in the `meter_spend` table.
Reach for it when a federated agent's token spend should count against the task that
started it.

### Review runs and the merge gate

`[review] enabled = true` turns on an engine that takes each queued review run (one per pull
request head) through its seats in order, reading each seat's outcome itself. A watch loop
queues a run for every open PR carrying `[review] label` in a repo listed under
`[review.repos."owner/name"]`. A repo with `merge = true` also has approved runs merged, pinned
to the reviewed commit, once CI is green. Its keys, and the checks made on a label, are in
`identity.toml.example`.

### Singleton vs primus

- **`role = "singleton"`**: one LLM persona with a tool-use loop. Reach for it for a
  question-and-answer persona such as archivist, architect or librarian.
- **`role = "primus"`**: plans, fans the work out to `subordinates` (an `implementer` is
  required, a `reviewer` optional), reviews and aggregates. It registers as
  `<name>-primus`. Reach for it for engineering work (mason, wright). Cost: several model
  calls per task, so it is slower and dearer than a singleton.

### The liminal integration

Set `[liminal] url` and give a persona `[personas.X.mcp] enabled = true`; that persona then
gets liminal's tools, scoped by `allowed_handlers` (omitted means every handler liminal
serves; `[]` means none) and `tool_extras`. Why: one gateway serves every persona's tools,
and a handler's credentials go from cabal's store to liminal (`cbl secret set <persona>
<handler> <KEY>`), not into the persona's prompt. Needs the `mcp` extra and a running liminal.

### techne memory

Set `[techne] db_path`. Every LLM persona then recalls matching memories into its prompt
before a task, and a persona with `commit_to_memory = true` also saves each question and
answer. `use_vectors = true` asks techne for vector recall as well (off by default; it
needs techne's vector support and a backfill).
Why: personas stop starting cold. Needs the `memory` extra. A relative `db_path` resolves
under `[cabal] data_dir`.

### The archivist (team memory) and `cbl archivist purge`

The archivist handler is a team knowledge store with its own SQLite file, separate from
techne: `archivist.publish` (dedups identical content, links or supersedes entries with the
same name), `archivist.search` (plain words, active entries only), `archivist.get` and
`archivist.schema`. Reach for it when several agents must share facts and decisions. It runs
inside liminal, so it needs `CABAL_ARCHIVIST_DB` on the liminal process.

To retire a fact that is no longer true, publish a replacement with `supersede: true`. To
remove text that must not be stored at all (personal data, a leaked secret), run
`cbl archivist purge <resource_name>` on the host that holds the file. It is a dry run until
you add `--apply`, which deletes every version, its index rows and relations, then VACUUMs.
No MCP tool or HTTP route reaches it.

### The four handlers ship unconfigured

The archivist, documents, patterns and code handlers load inside liminal, and none of them
has a corpus until you name one. On liminal 0.6.0 or later, `/healthz` reports each one as
`unconfigured` with the reason.

| Handler | Turn it on with | What you get |
|---|---|---|
| `archivist` | `CABAL_ARCHIVIST_DB` | Shared team memory: publish, search, get, schema. |
| `documents` | `CABAL_LIBRARIAN_DB` (needs techne) | Search over indexed documents; `refresh` is operator-only. |
| `patterns` | `CABAL_ARCHITECT_DB` (needs techne) | Search and record design patterns; `record` is operator-only. |
| `code` | no env var; a persona's `code.db_path` in `tool_extras` (needs techne) | Code search and blame; `refresh` has no repo list yet. |

Each also accepts a per-persona `<handler>.db_path` in `tool_extras`.

### Langfuse

Add a `[langfuse]` block (`host`, `public_key_env`, `secret_key_env`), set those two env
vars, and `pip install langfuse` (no extra pulls it). cabal then exports session start and
end and every LLM call to Langfuse. Why: see where a slow or wrong answer came from, in a
UI. Without it, cabal still writes tool-call and LLM-call rows to its own events table.

### `make up` / `make bootstrap` / `make demo` vs `cbl daemon up`

- **`cbl daemon up`** runs cabal alone, from a pip install. Reach for it to try cabal, or
  when liminal runs elsewhere. You read the bootstrap code from its log and `cbl login`.
- **`make up`, `make bootstrap`, `make demo`** build and run cabal and liminal together in
  Docker, from a clone with `techne` and `liminal` checked out beside it. `make bootstrap`
  (within 5 minutes of `make up`) mints the tokens and wires liminal; `make demo` is a
  6-step smoke test. Reach for it to see the whole platform work. Cost: Docker with buildx,
  and two sibling clones.

### `identity.toml`

One file holds `[cabal]` (operator id, bind, `bootstrap_code`, and two A2A v1.0 settings:
`public_url`, the base URL clients reach cabal at, without which no v1.0 agent cards are
served; and `front_door`, the persona whose card is served at
`/.well-known/agent-card.json`),
`[litellm]`, `[techne]`, `[liminal]`, `[langfuse]`, `[guardrails]`, `[groups]`, `[meter]`,
`[review]` and the personas. cabal
reads `--config` if given, else `./identity.toml` when the working directory holds cabal
state, else `$CABAL_HOME/identity.toml` (`~/.cabal`). Unknown keys are accepted silently and
unknown sections only log `identity.unknown_section`, so a typo shows up as a feature that
does not turn on. Start from `identity.toml.example` in the repo.

## How to install

Packages are distributed as `tenish-<name>` from git URLs or the private index.

The tenishai repos are private for now. Installing from the git URLs below needs a GitHub
account with read access to the tenishai organization, with git set up to use it (an SSH
key, or a token through a credential helper); without that, the install fails at the git
clone step.

- Everything wired, from git: `pip install -r requirements-sources.txt ".[full]"` in a
  clone. `requirements-sources.txt` pins the techne and liminal tags.
- Development: clone cabal, techne and liminal side by side, then
  `pip install -e ".[dev]" -e ../techne -e ../liminal`.
- Docker: `make up` as above, or the production `docker-compose.yml` (see the README; it
  needs four values in `.env` first).

Then set `CABAL_MASTER_KEY` and `CABAL_OPERATOR_ID` (or `[cabal] operator_id`) and run
`cbl daemon up`. Keep the master key: it encrypts the signing keys and secrets, and a
different key stops the daemon at startup.

## Where to go next

- `cbl guide` lists the guides this package carries.
- README sections: "Quickstart (development)", "Quickstart (Docker Compose)",
  "Configuration (identity.toml)", "Recovering a lost master key", "Removing an archivist
  entry", "Seeing the fleet".
- In the repo: `identity.toml.example` (every setting, commented), `docs/architecture.md`
  (the current design), `docs/outside-agents.md` (admitting an agent cabal doesn't own),
  `ARCHITECTURE.md` and `docs/arch/` (seam cards, for people changing the code).
