# Changelog

All notable changes to cabal are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.
Pull requests and issues are in tenishai/cabal.

## [Unreleased]

### Added

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
  Certificate of Origin), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SUPPORT.md`,
  issue and pull request templates, and a Dependabot config for pip and GitHub Actions.
  CI gains a DCO sign-off check on pull requests.
- **`AGENTS.md`**, a copyright notice in the README, and a Docker quickstart that works
  from a clean clone (tenishai/cabal#207, issues tenishai/cabal#201, tenishai/cabal#203,
  tenishai/cabal#204).

### Changed

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
  issues tenishai/cabal#36, tenishai/cabal#91, tenishai/cabal#161, tenishai/cabal#182).

### Fixed

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
- **A missed primus lookup names the real agent**: `--persona rook` fails with "no agent
  registered", and the message now says the agent registers as `rook-primus`
  (tenishai/cabal#228, issue tenishai/cabal#95).
- **techne and librarian state survive container recreation**: they were written to the
  container's writable layer instead of the `/data` volume (tenishai/cabal#217, issue
  tenishai/cabal#214).
- **The migration default URL follows the app's database default**, through one function
  that a test pins (tenishai/cabal#218, issue tenishai/cabal#215).

### Security

- **A wrong audience on `/auth/liminal-token` returns 403, not 401** (tenishai/cabal#227,
  issue tenishai/cabal#191), and the test that covers it now fails when the gate is
  removed.
- **A revoked colleague pass can no longer buy liminal tokens** (tenishai/cabal#224); see
  Added above.
- **`.env` is untracked** (tenishai/cabal#209). The master key it carried has been
  rotated and is dead. The old value remains in git history before the history rewrite;
  there is no re-encryption path, so changing the master key means `cbl reset-auth` and
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
