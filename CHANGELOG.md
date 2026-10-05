# Changelog

All notable changes to cabal are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.
Pull requests and issues are in tenishai/cabal.

## [Unreleased]

### Added

- **A2A v1.0 wire types** (issue tenishai/cabal#266, epic tenishai/cabal#272).
  `cabal.core.a2a.v1` holds the v1.0 data model, the SendMessage and GetTask request models
  and the SendMessage response, serialized per v1.0 (camelCase, ProtoJSON enums such as
  `TASK_STATE_COMPLETED` and `ROLE_USER`, ISO-8601 UTC timestamps). It converts cabal's
  internal Task to v1.0, including tasks still in flight, and inbound v1.0 messages to the
  internal form. Nothing serves them yet; the v1.0 endpoint is tenishai/cabal#267, and the
  legacy dialect is unchanged.
- **A2A v1.0 errors** (issue tenishai/cabal#269, epic tenishai/cabal#272).
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
  CI gains a DCO sign-off check on pull requests.
- **`AGENTS.md`**, a copyright notice in the README, and a Docker quickstart that works
  from a clean clone (tenishai/cabal#207, issues tenishai/cabal#201, tenishai/cabal#203,
  tenishai/cabal#204).

### Changed

- **README says which handlers the shipped stacks leave unconfigured** (part of
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
- **The distribution is now named `tenishai-cabal`**, because the PyPI name `cabal` belongs
  to an unrelated project. The import package (`cabal`) and the commands (`cbl`, `cabal`)
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
