---
card: arch-card:cabal-secrets
updated: 2026-11-03
anchor: src/cabal/core/secrets_backend.py sha=efab23f140e315ec24c695f392b73a7e80c92b71
anchor: src/cabal/core/secrets.py sha=d071247288bc276cc285844b863fc3b314f33195
anchor: src/cabal/core/routes/admin.py sha=0e7f5f0d6acb757b2e18f55cf1dead16d3a86808
---
# secrets — the platform's solo credential store (Fernet + SQLite)

WHAT / SEAM: `SecretsBackend` Protocol (core/secrets_backend.py `SecretsBackend`; async get/set/unset/list_entries/purge_for, keyed by (persona, handler)). ONE impl: `SqliteFernetBackend`, a thin wrapper over the module functions in core/secrets.py (set_secret/unset_secret/get_secrets_for/list_secret_entries/purge_secrets_for, `SecretEntry`, `merge_with_env`). Table `secrets` = models.py `Secret` (name PK, value_encrypted bytes, purpose). Encrypted with the same `CABAL_MASTER_KEY` that protects signing keys (core/auth/master_key.py).
OPS / FLOW:
- write: `cbl secret set|unset|list` (cli/main.py secret_app) → POST/DELETE/GET /admin/secrets… (routes/admin.py `set_secret`/`unset_secret`/`list_secrets`, operator scope) → `app.state.secrets_backend` (wired once in the core/app.py lifespan).
- read: GET /admin/secrets/{persona}/{handler} (admin.py `get_secrets`) → backend.get → env override `CABAL_SECRET_<persona>_<handler>_<KEY>` (prefix matched case-insensitively) via `merge_with_env` → `{persona, handler, credentials, caller, sources:{db,env}}`.
INVARIANTS:
- Row name is `<persona>:<handler>:<KEY>` (colon, `NAME_SEP`/`make_name` in secrets.py). WHY: persona/handler/KEY may all contain underscores; colons keep the DB key unambiguous (CABAL-DECISIONS D-015).
- ENV WINS over DB (D-015). WHY: operator one-off override without touching persisted state. Consequence: a stale shell var silently shadows a `cbl secret set`.
- KEY must match `^[A-Z][A-Z0-9_]*$` on write; persona/handler must match `[A-Za-z0-9_-]+` (`_normalize`, admin.py). WHY: they are spliced into an env-var prefix.
- DB persona/handler lookups are EXACT and case-sensitive prefix matches (`_name_starts_with`, substr compare), not SQL LIKE: LIKE treats `_`/`%` as wildcards and SQLite's ignores ASCII case, so persona `_lice` or `ALICE` once reached alice's secrets (cabal#252, #258). A handler-only list filter compares the parsed name in Python. Keep new queries off LIKE.
- List never returns values; get skips rows that fail decryption (InvalidToken) instead of failing the fetch. WHY: one row under a rotated key must not take down the whole credential set — but it also means a wrong master key reads as "no secret", not an error.
- Renaming a persona orphans its secrets; no migration by decision (D-016).
- `cbl reset-auth` keeps secrets unless `--purge-secrets` (cli/main.py `reset_auth_cmd`); they are unreadable after a master-key change anyway.
- Read auth = `_require_secrets_fetch`: aud ∈ {"cabal-core", gateway aud (`[liminal] jwt_aud`, default "liminal")} AND scope ∈ {operator, secrets-fetch}. Mint the service token with `cbl token mint-service --scope secrets-fetch` (POST /admin/token/mint-service, admin.py `mint_service_token`; it refuses operator scope and an `operator:` sub, #403). `--allowed-handler NAME` (repeatable) puts `cabal_tool_extras["liminal.allowed_handlers"]` on the token, as a persona's dispatch token carries it; without it the claim is absent and liminal allows every handler (#390).
CONSUMERS: liminal `CabalCoreCredentialResolver` (liminal src/liminal/server/credentials.py) calls GET /admin/secrets over HTTP with `LIMINAL_CABAL_TOKEN` and caches 15 min; so a `cbl secret set` can take up to 15 min to reach a handler through liminal. Handlers declare needed keys in their card's `credential_keys` (cabal's own four declare none).
API: the 4 /admin/secrets routes above; the Protocol for new backends.
NO PLUGIN DISCOVERY (cabal#235): the secrets_backend.py docstring now says so. Nothing reads an entry-point group `cabal.secrets_backends`; app.py hard-codes `SqliteFernetBackend`. A new backend today means editing app.py.
NOT HERE: no SOPS/Vault backend, and no code reference to ladon.
