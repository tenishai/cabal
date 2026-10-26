# Changelog

All notable changes to cabal are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.
Pull requests and issues are in tenishai/cabal.

## [Unreleased]

### Upgrading

- No database migration since 0.3.2, for cabal's database or the archivist's.
- litellm is pinned exactly to 1.104.2 (it was `>=1.92`). litellm 1.107.0 shipped a
  credential stealer on PyPI on 2026-10-21, and an open lower bound let any rebuild take
  whatever was newest. A litellm bump is now a reviewed line in `pyproject.toml`.
- Archivist search treats every token as a plain word. `AND`, `OR` and `NOT` used to reach
  FTS5 as operators when the query held no punctuation, and now match literally; a bare
  `{...}`, which raised an FTS5 syntax error then, is now plain words too. Tokens are still
  AND-ed.
- The archivist handler refuses wrong-typed fields instead of coercing them. `importance`
  must be a number from 0 to 1, search's `top_k` an integer, `source_thread` a string, and
  `tags`, `commands` and `refs` lists of strings; only `tags` still takes a comma-separated
  string. A client that sent a numeric string for `importance` or `top_k` (`"0.5"`, `"5"`),
  a comma-separated `commands` or `refs`, or an integer `source_thread` now gets a failed
  call naming the field. `top_k: 0` now returns 1 result; 0.3.2 fell back to 10.

### Added

- The archivist's `archivist.publish` tool takes an optional boolean `supersede`, default
  false (tenishai/cabal#348). `ArchivistStore.publish` could already retire the entries a
  new one replaces, but the tool never passed the flag, so over MCP a revised fact stayed
  active beside the one it replaced. A non-boolean value is refused.
- **`cbl archivist purge <resource_name>`** (tenishai/cabal#351) hard-deletes every
  version of an archivist entry, its full-text index rows and its relations, then runs
  FTS5 `optimize` and `VACUUM` so the text leaves the file. It does a dry run unless
  given `--apply`. It's for personal data and leaked secrets, and it runs only on the
  host that holds the database: no MCP tool or route reaches it. To retire an entry
  that is merely no longer true, supersede it with a tombstone instead.
- The archivist's publish takes an optional `supersede_scope` (tenishai/cabal#350).
  `"type"`, the default, keeps today's behavior: a supersede retires active entries with
  the same `resource_name` and `entry_type`. `"resource"` retires every active entry with
  the same `resource_name` whatever its type, for writers that use one name per fact and
  may revise its type (the techne publish lane). Available on `ArchivistStore.publish` and
  on the `archivist.publish` tool; an unknown value is refused.

### Fixed

- **The archivist, documents, patterns and code handlers say when they are unconfigured**
  (tenishai/liminal#191). Each refuses every call when it has no corpus, yet liminal's
  `/healthz` showed them "ready". Each now defines liminal's optional `status()`, which
  answers e.g. `unconfigured: CABAL_ARCHIVIST_DB is unset, so only a per-caller
  archivist.db_path can name a corpus`, and `/healthz` shows that instead. The code handler
  reads no environment variable, so as served by liminal it always reports unconfigured.
  liminal 0.6.0 is the first release that reads `status()`; it is optional there, so older
  liminal still loads the handlers and shows them ready, and the `tenish-liminal>=0.4` floor
  is unchanged.
- **A re-asserted archivist fact stays active through a gitsync round trip**
  (tenishai/cabal#367). Publishing A, superseding it with B and re-publishing A with
  `supersede=True` leaves A active in SQLite, but after `export_corpus` and
  `materialize_corpus` both came back superseded, because identity is the content hash
  and the markers point both ways. Materialize now follows the latest marker for each
  hash. gitsync has no caller in `src/` yet, so this was latent.
- **Concurrent identical archivist publishes no longer store duplicates**
  (tenishai/cabal#358). The dedup check, the insert and any supersede ran on separate
  connections, so identical publishes from two writers both saw no duplicate and both
  inserted; with `supersede_scope="type"`, simultaneous first publishes left two active
  rows. A publish now runs on one connection inside `BEGIN IMMEDIATE`, from the check
  through the insert, the supersede and any RELATED edges, so a second writer waits and
  then sees the first one's row. One process (today's liminal) was never affected.
- **Logged exceptions no longer carry frame locals, and logged database URLs hide the
  password** (tenishai/cabal#370). Neither cabal nor liminal configures structlog, so where
  rich is installed (the mash1 archivist image has it) `log.exception` rendered every
  frame's local variables: an archivist failure logged up to 80 characters of each
  payload string and the context's credentials. The four structlog `log.exception` calls
  now log the plain traceback text instead (`cabal.core.observability.failures.log_failure`).
  The `cabal.identity_path` and `cabal.migrations_failed` lines print the database URL with
  its password replaced by `***`.
- **Archivist search with `tags` no longer drops matching rows** (tenishai/cabal#362). The
  tag filter ran in Python after an FTS query limited to `top_k*4`, so with 50 `staging`
  rows and 1 `production` row matching "cache", `tags=["production"]` returned nothing. Each
  tag is now a SQL condition, so the limit applies after filtering.
- **A NUL or a lone surrogate in an archivist search no longer raises** (tenishai/cabal#365).
  Control characters become spaces and a lone surrogate becomes U+FFFD before the query is
  quoted; a surrogate pair sent as two code points is joined.
- **The dedup cache no longer reports a retired entry as the duplicate** (tenishai/cabal#353).
  After a supersede, re-publishing the retired content in the same process answered
  `duplicate_l0` with the retired id and stored nothing, so no active entry held it. An L0
  hit now counts only while its entry is active. With `supersede=True` such a publish
  re-asserts the content: it becomes a new active entry and retires the current one, as it
  already did after a restart.
- Unit CI now runs the archivist handler tests (tenishai/cabal#359). `tests/test_archivist.py`
  and four other handler test files skipped without liminal, which the unit job can't
  install, so handler changes merged with their tests never run in CI. A copy of liminal's
  `protocol` package in `tests/_vendor/` is put on the path when liminal is absent, and
  `tests/test_vendored_liminal_protocol.py` fails when an installed liminal's protocol
  differs from the copy. Test files that need liminal's server now skip on `liminal.server`.
- An archivist supersede that exactly matches an active entry still retires the other
  active entries in scope (tenishai/cabal#363). It returned `duplicate_l1` before any
  supersede ran, so when the techne publish lane re-sent an unchanged row with
  `supersede_scope="resource"`, an entry another writer had added under the same resource
  stayed active. The matched entry stays active, the outcome is still `duplicate_l0` or
  `duplicate_l1`, and `related_ids` now lists the entries it retired. `supersede_scope`
  applies as for any supersede. A match on an entry that is itself retired retires nothing.
- The archivist handler refuses a wrong-typed field instead of raising (tenishai/cabal#360).
  `importance: "high"`, `details: 5`, `top_k: "abc"` and similar payloads raised out of
  `dispatch`, and `source_thread: 7` was stored as an integer. Every optional field of
  publish and search, and the `archivist.db_path` override, is now type-checked, and the
  failed call names the field. `importance` must be a number from 0 to 1. `tags`, `commands`
  and `refs` must be lists of strings (`tags` still takes a comma-separated string);
  non-string items used to be coerced or dropped. Any other unexpected error is logged and
  returned as a failed call that names only the exception type. Each call logs one debug line `archivist.call` with the tool,
  outcome, entry id, resource, `written_by`, error and duration.
- An archivist publish with `importance: 0` stores 0 (tenishai/cabal#361). The handler
  read it with `or 1.0`, so 0 became 1.0, the highest importance.
- Archivist `get` says when an entry is retired (tenishai/cabal#357). The entry now carries
  `status` (`active` or `superseded`), and the result carries `superseded_by`, the ids of the
  entries that replaced it. Before, a session that fetched an old id got the retired fact with no
  sign it had been replaced. `ArchivistStore.superseded_by()` is new.
- Archivist search no longer fails on ordinary questions (tenishai/cabal#356). The query
  sanitiser quoted tokens only when the query held one of `"-^*():`, so
  `what's the build host?`, `mash1.local` or `docker/compose` reached FTS5 as raw syntax
  and raised `sqlite3.OperationalError`. Every token is now quoted. Tokens are still
  AND-ed, but `AND`, `OR`, `NOT`, `*`, `^`, `:` and `{}` are now plain words, not
  operators. The archivist handler also turns any database error into a failed tool
  call instead of raising.

## [0.3.2] — 2026-10-13

### Upgrading

- No database migration since 0.3.1, so rolling back to 0.3.1 is safe for the database.
- A missing-pair refusal carries a new `remedy` field (in `error.data` on the legacy
  JSON-RPC doors), and its message text changes. A client matching the old sentence needs
  updating. Status codes, error codes and the type of `detail` don't change.

### Changed

- A refused ask for a missing colleague pair says how to fix it (tenishai/cabal#330, closes tenishai/cabal#329). The
  message now reads "agent:A is not a colleague of B yet: agent:A's owner can invite B as
  a colleague, and B's owner accepts." in place of "... Ask in the room.", and the body
  carries `"remedy": "colleague_invite"` so a runtime can act on it without parsing the
  sentence: beside `detail` on `POST /a2a/tasks` and the A2A v1.0 route, and in the error's
  `data` on both legacy JSON-RPC doors (`POST /agents/{name}` and its `/stream` SSE error
  event). Status codes and error codes are unchanged, and `detail` is still a string.
  Refusals of every other kind carry no `remedy`.

## [0.3.1] — 2026-10-13

### Upgrading

- No database migration: the Alembic head is the same as 0.3.0's, so 0.3.0 and 0.3.1 run
  on the same database and you can move between them.
- Nothing changes unless you set `bootstrap_code`. The default, `log`, keeps writing the
  code to the log as 0.3.0 does. Rolling back to 0.3.0 ignores `bootstrap_code` and
  `$CABAL_BOOTSTRAP_CODE` and logs the code again.

### Changed

- The image's optional liminal (`WITH_MCP=1`) is now liminal 0.5.0 instead of 0.4.0, and
  `requirements-sources.txt` pins `v0.5.0` (tenishai/cabal#327). The handler API that cabal's handlers build on is
  unchanged in 0.5.0, and cabal's tests pass against it.

### Added

- `cabal.bootstrap_code` (or `$CABAL_BOOTSTRAP_CODE`) sets where the operator bootstrap code
  minted at each start goes (tenishai/cabal#326, closes tenishai/cabal#325). `log` is the default and today's behaviour.
  `file` writes it to `$CABAL_HOME/bootstrap_code` with mode 0600 and logs only the path.
  `off` mints no code. The code is a login for five minutes, and logs get copied and
  shipped, so a cabal that already has its operator can keep it out of them.

## [0.3.0] — 2026-10-12

### Upgrading

- **This release migrates the database. Back it up before you upgrade.** Alembic revision
  `d7e1f3a9b2c4_add_token_revocations` adds the `token_revocations` table
  (tenishai/cabal#221); it is the only revision added since 0.2.0. cabal runs
  `alembic upgrade head` at startup, so the first 0.3.0 start changes the database before
  it serves anything, and a failed migration stops the start (the 0.2.0 upgrade on
  2026-09-14 crash-looped on an Alembic problem). Stop cabal, then copy the SQLite file
  with any `-wal` and `-shm` files beside it: the path in `CABAL_DB_URL` when it is set
  (`/data/cabal.db` in the image), otherwise `data/cabal.db` under the state root.
  **0.2.0 will not start on a database 0.3.0 has migrated**: its startup `alembic upgrade
  head` cannot find revision `d7e1f3a9b2c4` and stops. To roll back, restore the backup,
  or run `alembic downgrade c4f0a2b18e77` with 0.3.0 still installed before switching
  back; the migration's `downgrade()` drops the table.
- **The distribution is now `tenish-cabal`** (tenishai/cabal#324). 0.2.0 was published as
  `cabal`, and main between the two releases called it `tenishai-cabal`
  (tenishai/cabal#241), a name no release used. The import package and the `cbl` and
  `cabal` commands are unchanged. Run `pip uninstall tenishai-cabal cabal` before
  installing 0.3.0 (pip skips a name that is not installed): pip treats the names as
  unrelated projects and would leave two distributions owning the `cabal` package. A
  dependency on `cabal[...]` or `tenishai-cabal[...]` becomes `tenish-cabal[...]`. An
  editable checkout must be reinstalled (`pip install -e ".[dev]"`); until then
  `cbl version` reports `0.0.0+unknown`, because `cabal.__version__` looks up only the new
  name. `v0.3.0` is the first tag that builds `tenish-cabal`.
- **liminal 0.4.0 as `tenish-liminal`, and techne 0.18.2 as `tenish-techne`**
  (tenishai/cabal#305, tenishai/cabal#324). The `mcp` and `full` extras need
  `tenish-liminal>=0.4`, the `memory` and `full` extras need `tenish-techne>=0.18.2`, and
  `requirements-sources.txt` pins liminal `v0.4.0` and techne `v0.18.2`. Run
  `pip uninstall tenishai-liminal liminal techne` first, for the same reason as above.
  A sibling-checkout build wants `../techne` at `v0.18.2`. The Dockerfile's `WITH_MCP=1`
  package-index path downloads `tenish-liminal==0.4.0` and checks the sha256 of the wheel
  built from the `v0.4.0` tag with `SOURCE_DATE_EPOCH` set to the tag commit's time (that
  build is reproducible). Until that wheel and `tenish-techne` 0.18.2 are uploaded to the
  index, an index-path image build fails; a sibling-checkout build does not.
- **Image builds that use the package index pass the token as a BuildKit secret**
  (tenishai/cabal#322): export `AR_TOKEN` and pass `--secret id=ar_token,env=AR_TOKEN`,
  not `--build-arg AR_TOKEN=...`. The Dockerfile no longer declares `ARG AR_TOKEN`, so a
  build that still passes the arg sends no token and a download from a private index
  fails.

### Added

- **Every refused dispatch is logged and audited** (tenishai/cabal#316, closes
  tenishai/cabal#315). The shared dispatch policy that the REST, legacy JSON-RPC and A2A
  v1.0 routes call logs a `dispatch.refused` warning with the caller, target, kind, reason
  and the check that refused (`token`, `revoked`, `audience`, `scope`, `sub`, `pair` or
  `signing_key`), never the token. A refusal after the token verifies also writes a
  `dispatch_refused` row to the `events` table; one before it is logged only, so an
  unauthenticated request makes no durable write. `PUT /admin/dispatch-pairs` logs
  `dispatch_pairs.replaced_empty` as a warning when it empties the table, because an empty
  table means open.
- **`--build-arg WITH_MCP=1` puts liminal in an image built from the package index**
  (tenishai/cabal#306). With `EXTRA_INDEX_URL` it downloads the pinned liminal wheel from
  the index alone, checks `LIMINAL_WHEEL_SHA256` and installs it, as the techne path does.
  A sibling `--build-context liminal=...` still wins; `WITH_MCP=1` with neither stops the
  build. `gojira.yaml` passes it.
- **Gojira deploy config**: `gojira.yaml`, the `/data` PVC and the generated Deployment,
  Service, PodMonitoring and rules under `k8s/base`, a `dev` overlay, and the generated
  `.github/workflows/gojira-ci.yaml` (tenishai/cabal#240, tenishai/cabal#256,
  tenishai/cabal#304, tenishai/cabal#312).
- **Architecture cards**: `ARCHITECTURE.md`, seam cards under `docs/arch/`, and
  `scripts/check_arch_cards.py`, which CI runs so a seam that changes without its card
  fails the build (tenishai/cabal#232).
- **A2A reason code 703, `CODE_TOOL_BLOCKED`**, mapped to the `failed` state, with the
  same name and meaning as daedalus's (tenishai/cabal#243, part of tenishai/cabal#237).
  Nothing emits it yet.

- **The A2A compatibility kit in CI** (tenishai/cabal#299, issue tenishai/cabal#261, epic tenishai/cabal#272). A new
  workflow, `a2a-tck`, runs a2aproject/a2a-tck at a pinned commit on a GitHub-hosted runner
  against a test-only `tck` persona, which acts out the kit's scenarios and exists only when
  `$CABAL_TCK_PERSONA` is `1`. A proxy adds the token the kit cannot send (tenishai/cabal#271).
  The job fails on any requirement the kit fails that `scripts/a2a_tck/baseline.json` does not
  list with a reason. Fixes the first run found: v1.0 agent cards now carry `Cache-Control`
  and `ETag`; CancelTask cancels a task paused in `input-required` or `auth-required`; and
  v1.0 task metadata no longer carries cabal's internal `cabal_*` keys, with the reason keys
  renamed `cabalReasonCode`, `cabalReasonMessage` and `cabalEscalatedFrom`.
- **A2A v1.0 returnImmediately** (tenishai/cabal#298, issue tenishai/cabal#277, epic tenishai/cabal#272).
  A SendMessage that starts a new task with `configuration.returnImmediately: true` answers at once with the task in
  `TASK_STATE_SUBMITTED`; the dispatch runs detached and the client polls GetTask, which shows
  `TASK_STATE_WORKING` and then the end state. CancelTask reaches it. At most
  `cabal.detached_task_limit` (16) run at once; more are refused with `-32004`. On shutdown each
  gets `cabal.detached_drain_seconds` (10) to stop before it is cancelled.
  At startup, every task still `submitted` or `working` is marked failed, "interrupted by a
  restart", which also clears the tasks a crash used to leave stuck; this assumes one cabal
  process per database, as today. A dispatch, blocking through `router.dispatch` or detached
  through `start_detached`, now records `working` before the persona is called;
  the streaming path and operator resume do not yet.
- **A2A v1.0 conversations by contextId** (tenishai/cabal#297, issue tenishai/cabal#276, epic tenishai/cabal#272).
  SendMessage without a `contextId` now gets one generated and returned on the task. One with
  a `contextId` starts a new task in that conversation, and the persona sees the conversation
  so far as the task's history: the caller's latest earlier task in that context on that
  persona with a stored result, its history plus its reply, newest `cabal.context_replay_messages` messages (40 by
  default, 0 for none). Only the caller's own tasks are replayed. A turn cabal ended without
  raising (a cooperative cancel, a router guardrail block on the blocking path, a persona's
  own budget or policy refusal) is replayed with the caller's message and the reason as the reply.
  A turn that raised (a guardrail termination such as the router's delegation budget, a block
  on the streaming path, an error) stores no result and is not replayed. A SendMessage naming a
  `taskId` now gets `-32001` for an unknown task, `-32602` for a `contextId` that differs from the one stored on the task, and
  `-32004` for a finished task or, until tenishai/cabal#145, an unfinished one.
  The router now restores a dispatched task's `contextId` when the persona's reply drops it,
  on every dispatch path, so the stored task keeps its conversation key, and stores a reply
  whose history is no longer than its input (cabal's own cancel and block replies) with the
  input history and the incoming message; the streaming path now stores them too.
- **How to admit an outside A2A agent** (tenishai/cabal#295, issue tenishai/cabal#271). `docs/outside-agents.md`
  is the operator procedure #271 decided: one `cbl token mint-service` token per persona the
  outsider may call (`--sub agent:ext-<name> --scope dispatch --aud <agent>`, the persona's bulletin name), handed over with
  the persona's card URL, and revoked per subject. `tests/test_a2a_v1_outside_agent.py`
  checks that path end to end.
- **A2A v1.0 cards declare bearer-JWT auth** (tenishai/cabal#294, issue tenishai/cabal#271, epic tenishai/cabal#272).
  Every persona card, and so the front-door card, declares one security scheme, `bearer`
  (`httpAuthSecurityScheme`, `scheme: "Bearer"`, `bearerFormat: "JWT"`), and a security
  requirement naming it. Outside agents get that JWT from the cabal's operator, as #271
  decided. Default-value removal for signing now reaches into these fields, including map
  values, so a verifier that fills an empty scope list or description back in still gets the
  signed content.
- **A2A v1.0 content types on SendMessage** (tenishai/cabal#291, issue tenishai/cabal#274, epic tenishai/cabal#272).
  A message part whose media type is not in the persona card's `defaultInputModes`
  (`text/plain`, `application/json`) gets `ContentTypeNotSupportedError` (`-32005`). A part's
  media type is its `mediaType` when set, compared without parameters and case, and otherwise
  the type its kind implies. `historyLength` on SendMessage and GetTask, the issue's other half,
  shipped with tenishai/cabal#267 and now has tests for an omitted value, 0, and values smaller
  and larger than the history.
- **A2A v1.0 agent cards for each persona** (tenishai/cabal#288,
  tenishai/cabal#292, issue tenishai/cabal#270, epic tenishai/cabal#272).
  `GET /agents/{name}/.well-known/agent-card.json` serves the agent's v1.0 AgentCard with
  one embedded signature: `typ: JOSE`, RS256, by cabal's active signing key, over the card
  with default values and `signatures` removed and the rest in RFC 8785 form (spec 8.4).
  The card's one interface is `{public_url}/agents/{name}/a2a` (JSON-RPC, A2A 1.0); its
  capabilities (streaming, push notifications, extended card) are all off; its input and
  output modes are `text/plain` and `application/json`. It is built from the agent's legacy
  card, which is unchanged at `/agents/{name}/.well-known/agent-card`.
  The card needs the new `cabal.public_url` setting (or `$CABAL_PUBLIC_URL`), the URL
  clients reach cabal at: without a valid one the route answers 503 rather than publish a
  wrong interface URL. A federated peer has no card here.
  `/directory` lists each agent's card URL as `agent_card_url` (null without a public URL,
  and for a federated peer).
  `cabal.core.a2a.v1_card` holds the card model, the RFC 8785 serializer (checked against
  the RFC's own number samples), signing, and verification for v1.0 cards.
  `/.well-known/agent-card.json`, the host's discovery card, serves the card of the persona
  named by the new `cabal.front_door` setting (or `$CABAL_FRONT_DOOR`), with a line in its
  description pointing at `/directory`. With no front door it answers 404; with a front door
  that names an agent without a card here, or without a public URL, 503. cabal-core has no
  endpoint of its own to name, and a well-known card must name one. The legacy stub at
  `/.well-known/agent-card` is unchanged.
- **A2A v1.0 ListTasks and CancelTask** (tenishai/cabal#286, issue tenishai/cabal#268, epic tenishai/cabal#272).
  `ListTasks` on `POST /agents/{name}/a2a` returns the caller's own tasks on that persona
  (every task on it, for the operator), newest status first, with cursor pagination
  (`pageSize` 1 to 100, default 50; `nextPageToken` is `""` on the last page) and the
  `status`, `statusTimestampAfter`, `contextId` and `historyLength` options. Artifacts are
  left out unless `includeArtifacts` is true. A task still in flight has no stored Task, so
  a `contextId` filter does not match it.
  `CancelTask` cancels a task running in this process through the `AbortCoordinator`, waits
  up to 5 seconds for its dispatch to record the end, and returns the task. A finished task,
  one dispatched outside this process (a federated peer, say), and one with nothing running behind it get
  `TaskNotCancelableError` (`-32002`); a task the caller may not see gets `-32001`.
  The four push notification config methods now get `-32003` and `GetExtendedAgentCard`
  `-32004`, matching what the cards will declare (tenishai/cabal#270).
  A non-operator token with no `sub` now owns no task on this path, so it cannot see a task
  whose caller was not recorded.
- **A2A v1.0 SendMessage and GetTask** (tenishai/cabal#282, issue tenishai/cabal#267, epic tenishai/cabal#272).
  `POST /agents/{name}/a2a` now serves `SendMessage` and `GetTask` in the v1.0 dialect.
  Both need a bearer token; a request without one gets HTTP 401 with `WWW-Authenticate: Bearer`.
  `SendMessage` dispatches to the persona in the path with the same token policy as the
  legacy `message/send`, and returns the task the persona returns. A `tenant` must name that persona.
  Push notification config gets `-32003`; `returnImmediately` (tenishai/cabal#277) and a
  `message.taskId` (tenishai/cabal#276) get `-32004`; `raw` and `url` parts get `-32005`.
  `GetTask` answers a missing task, another caller's task, and another persona's task with
  the same `TaskNotFoundError` (`-32001`), so it never reveals that a task exists.
  `SendStreamingMessage` and `SubscribeToTask` get `-32004`; other methods get `-32601`.
  The token policy moved to `routes/_dispatch_auth.py`, shared by both dialects; the legacy
  routes keep their error codes.
- **A2A v1.0 version gate** (tenishai/cabal#281, issue tenishai/cabal#265, epic tenishai/cabal#272). A new
  per-persona path, `POST /agents/{name}/a2a`, accepts `A2A-Version: 1.0` (header or
  `A2A-Version` query parameter; a patch number is ignored) and answers any other version,
  including none, with `VersionNotSupportedError` (`-32009`) once the body parses as a JSON-RPC request. Behind the gate are
  `SendMessage` and `GetTask` (tenishai/cabal#267, above). The
  legacy routes still accept requests with no `A2A-Version`, which is every current client;
  a well-formed request with a non-empty `A2A-Version` now gets `-32009` on `/agents/{name}`
  and `/agents/{name}/stream`, and HTTP 400 on `/a2a/tasks`. A malformed body still gets its
  parse or validation error first.
- **A2A v1.0 wire types** (tenishai/cabal#278, issue tenishai/cabal#266, epic tenishai/cabal#272).
  `cabal.core.a2a.v1` holds the v1.0 data model, the SendMessage and GetTask request models
  and the SendMessage response, serialized per v1.0 (camelCase, ProtoJSON enums such as
  `TASK_STATE_COMPLETED` and `ROLE_USER`, ISO-8601 UTC timestamps). It converts cabal's
  internal Task to v1.0, including tasks still in flight, and inbound v1.0 messages to the
  internal form. Nothing serves them yet; the v1.0 endpoint is tenishai/cabal#267, and the
  legacy dialect is unchanged.
- **A2A v1.0 errors** (tenishai/cabal#280, issue tenishai/cabal#269, epic tenishai/cabal#272).
  `cabal.core.a2a.v1_errors` defines the nine A2A v1.0 errors with their codes (`-32001` to
  `-32009`) and the `google.rpc.ErrorInfo` that spec 9.5 requires in the error's `data`, plus
  helpers for answering authentication and authorization with HTTP 401 (with
  `WWW-Authenticate: Bearer`) and 403. The v1.0 interface (tenishai/cabal#267) will use them;
  nothing serves them yet. cabal's legacy codes reuse `-32001` to `-32003` for other
  meanings, so they stay on the legacy routes only.
- **Revoke every token a subject holds** (tenishai/cabal#221). `POST /admin/token/revoke`
  (operator scope) records that tokens for a subject issued before a cutoff are refused.
  Every verification site on the dispatch path (REST, JSON-RPC, the broker router) and
  the scope gate check it, and the cutoffs persist in the new `token_revocations` table,
  so a restart remembers them. The cutoff only moves forward. The follow-up
  tenishai/cabal#224 closed two holes: the liminal-token exchange now refuses a revoked
  colleague pass, and a revoke takes effect at the start of the next second, so a token
  minted in the same second as the revoke no longer survives it.
- **`CABAL_DATA_DIR` overrides `cabal.data_dir`** (tenishai/cabal#217, issue
  tenishai/cabal#214). The container image sets it to `/data`.
- **`SECURITY.md`**: how to report a vulnerability privately, what is in scope
  (tenishai/cabal#225, follow-ups in tenishai/cabal#227).
- **Community files for the public release**: `CONTRIBUTING.md` (including the Developer
  Certificate of Origin), `CODE_OF_CONDUCT.md` (Contributor Covenant 3.0), `SUPPORT.md`,
  issue and pull request templates, and a Dependabot config for pip and GitHub Actions.
  CI gains a DCO sign-off check on pull requests (tenishai/cabal#241).
- **`AGENTS.md`**, a copyright notice in the README, and a Docker quickstart that works
  from a clean clone (tenishai/cabal#207, issues tenishai/cabal#201, tenishai/cabal#203,
  tenishai/cabal#204).

### Changed

- **The distribution is named `tenish-cabal`**: every released tenish Python distribution
  is `tenish-<project>`, as techne is `tenish-techne` and liminal `tenish-liminal`.
  pyproject's `name`, `cabal.__version__`'s lookup, the install hints in error messages,
  the README and the architecture cards change; the import package, the `cbl` and `cabal`
  commands and the GitHub URL do not (tenishai/cabal#324). See Upgrading.
- **liminal 0.4.0, under its new distribution name `tenish-liminal`.** The `mcp` and
  `full` extras need `tenish-liminal>=0.4`, and `requirements-sources.txt` pins the
  `v0.4.0` tag (tenishai/cabal#324). The Dockerfile's index path pins `tenish-liminal`
  0.4.0; see Upgrading.
- **directory lists only the targets a colleague may dispatch to** (tenishai/cabal#323,
  closes tenishai/cabal#321). `GET /directory` filters a non-operator caller's roster
  through the dispatch-pairs table, keyed by the token's `sub`, so it no longer lists
  agents the dispatch would refuse. An empty table is still open, and an operator still
  sees every agent.
- **`pyproject.toml` declares `joserfc`**, which the code imports, instead of `authlib`,
  which nothing imported; `TechneMemoryStore` degrades without techne instead of raising
  `ImportError`; the unused `mcp_proxy.build_token_provider` is removed
  (tenishai/cabal#243, part of tenishai/cabal#235).
- **The copyright holder is "tenish and the cabal contributors"** in NOTICE and the
  README, and pyproject's `authors` is `tenish`. The licence (AGPL-3.0-or-later) is
  unchanged (tenishai/cabal#279).
- **The README says how cabal was built, and CONTRIBUTING.md gives the policy for
  AI-assisted contributions** (tenishai/cabal#308).
- **techne 0.18.2, under its new distribution name `tenish-techne`** (tenishai/cabal#305). The `memory` and
  `full` extras need `tenish-techne>=0.18.2`, `requirements-sources.txt` pins the `v0.18.2`
  tag, and the Dockerfile's index path downloads `tenish-techne==0.18.2` and checks its
  wheel hash. The import package and the `techne` command are unchanged. Run
  `pip uninstall techne` before reinstalling: pip treats the two names as unrelated
  projects and would leave the old one installed beside the new one. A sibling build wants
  `../techne` at `v0.18.2`.
- **README says which handlers the shipped stacks leave unconfigured** (tenishai/cabal#247, part of
  tenishai/cabal#129). `archivist`, `documents`, `patterns` and `code` need corpus paths
  that no compose or k8s file sets; the README now names the env vars and the gap.
- **Operator state defaults to `~/.cabal`, not the checkout** (tenishai/cabal#212, issue
  tenishai/cabal#211). Resolution order: an explicit `--config`; then `./identity.toml`
  if it exists (existing installs keep working in place); then
  `<CABAL_HOME or ~/.cabal>/identity.toml`. The CLI token follows `CABAL_HOME` too
  (tenishai/cabal#228, issue tenishai/cabal#216).
- **The image can build without a sibling techne checkout** (tenishai/cabal#239). Pass
  `--build-context techne=...` as before, or `--build-arg EXTRA_INDEX_URL` (and
  `AR_TOKEN` for a private index) and techne installs from that index alone. With
  neither, the build says so and stops.
- **techne is pinned by version and wheel hash** (tenishai/cabal#249, tenishai/cabal#251).
  The index path downloads `TECHNE_VERSION` as a wheel only and stops the build when its
  sha256 differs from `TECHNE_WHEEL_SHA256`. On the sibling path,
  `scripts/check-techne-sibling.sh` (run by `make build`, `make rebuild` and `make up`)
  compares `../techne`'s HEAD with the `v$TECHNE_VERSION` tag, and the Dockerfile refuses a
  sibling whose version differs. `TECHNE_ANY=1` skips both checks, for developing techne
  and cabal together.
- **The distribution was renamed `tenishai-cabal`** (tenishai/cabal#241), because the PyPI
  name `cabal` belongs to an unrelated project; 0.3.0 ships it as `tenish-cabal`, above. The import package (`cabal`) and the commands (`cbl`, `cabal`)
  are unchanged. Anything that looks cabal up by distribution name must change
  (`pip show tenishai-cabal`, `pip install 'tenishai-cabal[mcp]'`,
  `importlib.metadata.version("tenishai-cabal")`), and existing environments should
  `pip uninstall cabal` before installing, so two distributions do not own one package.
- **techne floor and source pin move to 0.17.0** (tenishai/cabal#195, issue
  tenishai/cabal#194).
- **liminal 0.3.0 and techne 0.18.0** (tenishai/cabal#253). liminal 0.3.0 is published as
  `tenishai-liminal`, so the `mcp` and `full` extras name `tenishai-liminal>=0.3`, and an
  environment that has the old `liminal` distribution should `pip uninstall liminal` first.
  The techne floor is 0.18.0, and `requirements-sources.txt` pins techne v0.18.0 and
  liminal v0.3.0. With `TECHNE_ANY=1`, compose now builds the liminal image against any
  `../techne` too.
- **Pinned and bounded CI**: every third-party Action is pinned to a commit SHA, `unit`
  runs on a self-hosted pool behind techne's fork guard with fork PRs routed to a hosted
  twin (tenishai/cabal#219), and the dispatch-only jobs clone techne and liminal without
  a token (tenishai/cabal#223). A test pins the fork guard on every self-hosted job
  (tenishai/cabal#227, issue tenishai/cabal#220).
- **`identity.mtplx.toml` is removed** (tenishai/cabal#210, issue tenishai/cabal#206). It
  was one operator's personal profile; what it taught is in `identity.toml.example`.
- **Docs no longer cite a design document this repo lacks** (tenishai/cabal#208, issue
  tenishai/cabal#202); the proposals README says which proposals are stale. Remaining
  mentions of that document now say it is not in this repo, and no longer give a path.
- **TUI and CLI error messages** are honest and clean at boot, and the delegation-budget
  block message no longer says "conversation" on the caller path (tenishai/cabal#228,
  issues tenishai/cabal#36, tenishai/cabal#91, tenishai/cabal#161).

### Fixed

- **The delegation-edge guardrail judges `agent:<name>` callers** (tenishai/cabal#301,
  closes tenishai/cabal#189). With `[guardrails] delegation_edges = true` it refused every
  room colleague and outside agent, whose tokens carry no persona claim. The new
  `[guardrails] caller_edges` table, keyed by the full `agent:<name>` sub, says which
  personas such a caller may reach; a key without the `agent:` prefix fails config load.
- **The TUI starts without a token file** (tenishai/cabal#302, closes tenishai/cabal#90).
  `cabal --persona X` boots cabal in-process and printed no bootstrap code, so a first run
  could not log in. With no token passed and no token file it now mints a one-hour operator
  token from the signing key for each message and writes nothing to disk. A token that is
  present but fails verification is still reported.
- **The heartbeat scheduler survives a failed ping** instead of stopping silently at the
  first one (tenishai/cabal#243, closes tenishai/cabal#230).
- **`make up` gives a healthy cabal-dev on a clean clone** (tenishai/cabal#242). The
  Dockerfile takes an optional `liminal` build context, and both compose files pass
  `../liminal`, so the `[liminal] url` check from tenishai/cabal#126 finds the package. The
  README quickstarts run as written.
- **Dispatch to a federated target through JSON-RPC or the TUI** (tenishai/cabal#285, closes tenishai/cabal#234). The
  legacy JSON-RPC route, the A2A v1.0 route and the TUI's in-process client minted a federated
  target's dispatch token for its bare name, and the router refused that token. They now mint
  the target's `federation_audience`, as `POST /a2a/tasks` always did. The REST route now
  calls the same policy as the JSON-RPC routes (`routes/_dispatch_auth.py`), so the network
  routes share one copy of it; the REST route's answers are unchanged. A federated target with no `federation_audience` is
  refused as a server-side error on every network route, and with a `BrokerError` in the TUI.
- **cabal no longer claims A2A v1.0** (tenishai/cabal#263, refs tenishai/cabal#261). Its
  code and docs said "A2A v1.0". The A2A JSON-RPC route each agent serves uses A2A's
  pre-1.0 method names (`message/send`, `tasks/get`, as in A2A 0.3.0), not v1.0's
  `SendMessage` and `GetTask`, and every JSON-RPC and stream call needs a cabal-issued
  token. Its agent card matches no A2A version: for the personas it signs, cabal serves
  `{"jws": ..., "card": ...}` at `/agents/{name}/.well-known/agent-card`, where A2A 0.3.0
  and v1.0 serve the AgentCard at `/.well-known/agent-card.json` with a `signatures` array
  inside it, so an A2A client cannot discover a cabal agent. cabal's own dispatch between
  processes does not use this route; it posts Tasks to `/a2a/tasks` (see
  tenishai/cabal#264). Whether to move to v1.0 is open in tenishai/agora discussion 50.

- **`make bootstrap` explains an expired code and waits for liminal** (tenishai/cabal#248).
  Exchanging an expired or used bootstrap code used to end in a bare `JSONDecodeError`;
  it now says the code expired and how to get a new one. The script also waits for
  liminal's `/healthz` after restarting it, so `make demo` can run straight after. The
  README says to bootstrap within 5 minutes of `make up` and adds `sudo apt update`
  before the apt installs. Found by the 2026-10-02 clean-machine run (tenishai/agora#47).
- **Persona sync is generation-safe** (tenishai/cabal#229, issue tenishai/cabal#149):
  `sync_personas` no longer registers at a hardcoded generation 1, so the documented
  recovery from absorb works. The same PR adds a shared honesty rule for personas
  (issue tenishai/cabal#138), configurable archivist and librarian stores, and
  regression tests for the primus class-mismatch guard and the caller-window knob
  (issues tenishai/cabal#68, tenishai/cabal#160).
- **The `[liminal]` availability check now runs whenever `[liminal] url` is set**, including in a config with no personas
  (tenishai/cabal#229, issue tenishai/cabal#126), and `k8s/README.md` documents rolling
  config and image together so a persona rename does not degrade silently (issue
  tenishai/cabal#114).
- **A missed primus lookup names the real agent**: `--persona mason` fails with "no agent
  registered", and the message now says the agent registers as `mason-primus`
  (tenishai/cabal#228, issue tenishai/cabal#95).
- **techne and librarian state survive container recreation**: they were written to the
  container's writable layer instead of the `/data` volume (tenishai/cabal#217, issue
  tenishai/cabal#214).
- **The migration default URL follows the app's database default**, through one function
  that a test pins (tenishai/cabal#218, issue tenishai/cabal#215).

### Security

- **The package-index token is a BuildKit secret, not a build arg** (tenishai/cabal#322,
  part of tenishai/cabal#250 with tenishai/gojira#109). Docker prints build args in the
  build log and keeps them in the stage's metadata and cache. The dependency `RUN` now
  mounts `ar_token` and reads it only on the index branch, and `gojira-ci.yaml` passes
  `--secret id=ar_token,env=AR_TOKEN`. See Upgrading.
- **`documents.refresh` and `patterns.record` are operator-only** (tenishai/cabal#245,
  closes tenishai/cabal#233). Both claimed an operator check and had none; their card
  entries now set `requires_operator: true`, which liminal enforces from v0.3.0
  (tenishai/liminal#122).
- **Dispatch re-mint refuses a token with no `sub`** instead of minting it as `operator`:
  REST answers 401, JSON-RPC `ERROR_UNAUTHORIZED`, and the in-process client raises
  `BrokerError` (tenishai/cabal#243, part of tenishai/cabal#184).
- **Secret lookups match the persona and handler exactly** (tenishai/cabal#258, issue
  tenishai/cabal#252). They used SQL `LIKE`, where `_` and `%` are wildcards and SQLite
  ignores ASCII case, so a request for persona `_lice` or `ALICE` returned alice's secrets
  and a purge for that name deleted them. Fetching, listing and purging now compare names
  exactly and case-sensitively. The `CABAL_SECRET_*` env override still matches
  case-insensitively.
- **A wrong audience on `/auth/liminal-token` returns 403, not 401** (tenishai/cabal#227,
  issue tenishai/cabal#191), and the test that covers it now fails when the gate is
  removed.
- **A revoked colleague pass can no longer buy liminal tokens** (tenishai/cabal#224); see
  Added above.
- **`.env` is untracked** (tenishai/cabal#209). The master key it carried has been
  rotated and is dead. The old (dead) value is still in this repository's history, in
  the commits from tenishai/cabal#11 up to tenishai/cabal#209. There is no re-encryption path, so changing the master key means `cbl reset-auth` and
  re-minting `LIMINAL_CABAL_TOKEN`.
- **The Docker runtime stage runs `apt-get upgrade`**, refreshable with `APT_REFRESH`, so
  base packages reach current Debian security revisions even from a cached base layer;
  CI builds with `--pull` (tenishai/cabal#192).
- **A placeholder host replaced a private LAN address** in a tracked example identity
  (tenishai/cabal#199, since superseded by tenishai/cabal#210).

## [0.2.0] — 2026-09-14

- **Dispatch pairs**: the switchboard checks who may call whom (tenishai/cabal#127), plus
  a `colleague` scope and a directory for agents (tenishai/cabal#124), and a gateway
  token exchange that swaps a standing identity without dialing (tenishai/cabal#143).
- **Guardrails**: delegation depth, breadth and budget limits (tenishai/cabal#170,
  tenishai/cabal#169, tenishai/cabal#150), persisted budgets that survive a restart
  (tenishai/cabal#175), delegation allowed only along declared edges
  (tenishai/cabal#163), and a refusal that cannot be rephrased past ends the task
  (tenishai/cabal#167).
- **Operator controls**: an operator-settable breadth ceiling (tenishai/cabal#178), a
  conversations filter by caller (tenishai/cabal#128), task metadata on published events
  (tenishai/cabal#190), and the gateway audience read from config (tenishai/cabal#188).
- **Visibility**: live agent sessions in `cbl status` (tenishai/cabal#148); provider
  kwargs from `identity.toml` reach the LLM call (tenishai/cabal#131).
- **Fixes**: a truncated LLM response is a failure, not a completed task
  (tenishai/cabal#135, tenishai/cabal#137); a clean install can migrate and dispatch
  (tenishai/cabal#165); stale build artifacts in the Docker image (tenishai/cabal#123);
  a master-key mismatch is diagnosable (tenishai/cabal#130); the archivist stops writing
  its own answers into the store it searches (tenishai/cabal#166).
- **Renames**: limenal to liminal (tenishai/cabal#116); the primus personas rook to
  mason and jackdaw to wright (tenishai/cabal#113).

## [0.1.0] — 2026-07-10

First tagged release. A FastAPI broker that dispatches work between agents over A2A,
with JWT-scoped auth and SQLite underneath, plus:

- capability-based dispatch, durable checkpointed execution with task resume, and
  cooperative cancellation across nested dispatch (AbortGraph);
- composable guardrails: validators, a doom-loop detector, and fail-closed behaviour
  (tenishai/cabal#56, tenishai/cabal#58, tenishai/cabal#64);
- personas: Librarian, Codex, Archivist (with git-backed team memory sync), and the
  primus base with a general-engineering primus (tenishai/cabal#67);
- a Textual chat console (tenishai/cabal#34), activity-log observability with token and
  cost on LLM calls, and a patterns handler over techne's Architect store;
- optional techne and liminal extras with pinned git sources for clean-machine installs
  (tenishai/cabal#80); the licence changed to AGPL-3.0-or-later.
