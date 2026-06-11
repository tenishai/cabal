# Cabal Overnight Build Status — 2026-06-10/11

Autonomous-mode summary for morning review. See [CABAL-DECISIONS.md](CABAL-DECISIONS.md)
for the per-decision rationale log.

## Headline

- **GitHub**: https://github.com/dimartin/cabal — 13 commits, all on `main`.
- **Tests**: 115 passing, ~6s wall clock.
- **End-to-end loop works**: `cbl daemon up` → `cbl login` → `cbl dispatch echo …` returns a real Task with bookkeeping rows, signed JWT, session_start/end hooks fired, heartbeat ticking.
- **Phase 1 (per cabal-handoff.md §Phase 1 Build Order)**: complete.
- **Phase 2 foundation**: LiteLLM client wrapper + circuit breaker + LLM persona base class landed; Langfuse/OTLP hook adapter not yet wired (registry is ready for it).

## What landed (in build order)

| # | Layer | Commit topic | What it ships |
|---|---|---|---|
| 1 | scaffold | initial scaffold | pyproject, Typer CLI shell, FastAPI smoke, decision log skeleton |
| 2 | db | SQLAlchemy + Alembic | All 7 tables; composite PK on agents; conversations FK; schema constraint tests |
| 3 | auth | RS256 keypairs + JWKS | Master-key encryption + signing_keys table + /.well-known/jwks.json |
| 4 | a2a | AgentCard sign/verify + cache | JWS over JCS-canonicalized AgentCard; verifier cache with kid invalidation (§6.12 inv 1) |
| 5 | bulletin + jwt | agent CRUD + JWT mint/verify | get_active uses partial index; rotation flow tested; JWT audience pinning + leeway |
| 6 | broker | dispatch router (§6.12) | All 4 invariants enforced + tested in BOTH in-process and HTTP modes |
| 7 | e2e | bootstrap code + /a2a/tasks | Bootstrap code → operator JWT → dispatch returns echoed Task |
| 8 | cli | cbl login + dispatch | TokenStore (~/.cabal/token, 0600); httpx client; live-daemon CLI test |
| 9 | llm | LiteLLM wrapper + breaker | Single-probe HALF_OPEN; error classification; LITELLM_PRE/POST hooks fire |
| 10 | a2a + hooks | reason codes + session hooks | 700/701/702/704 codes; session_start/end fire on root dispatches only |
| 11 | identity | identity.toml → bulletin | Personas declared in TOML auto-register with signed AgentCards |
| 12 | agents | LLMPersona base | LiteLLMClient-backed LocalHandler with system_prompt + history replay |
| 13 | ops | heartbeat + admin/agents | Background pinger; `cbl status` table view; operator-scope gate |

## What the morning operator can demo

```bash
# Terminal 1
cd cabal
source .venv/bin/activate
export CABAL_MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
cbl daemon up                       # → boots, logs bootstrap code

# Terminal 2
cbl login --code <CODE_FROM_LOG>     # → ~/.cabal/token saved
cbl dispatch echo "hello cabal"      # → "[completed] echo: hello cabal"
cbl status                           # → table of agents + last_heartbeat
```

External curl loop (no CLI) verified end-to-end with full payload inspection.

## §6.12 Dispatch Router invariants — implementation status

| Invariant | File | Tested? |
|---|---|---|
| 1. AgentCard signature verify + cache + kid invalidation | `core/a2a/card.py` | ✓ 5 tests |
| 2. Caller JWT mint+verify on every dispatch (in-process too) | `core/auth/jwt.py` + `core/broker/router.py::_prepare` | ✓ 3 tests across modes |
| 3. Broker bookkeeping participation, both modes | `core/broker/bookkeeping.py` + `router._dispatch_*` | ✓ 2 dedicated tests + parity test |
| 4. Trace context propagation via `task.metadata.cabal_trace_id` | `router._ensure_trace` | ✓ generation + preservation tests |

## Decisions made tonight (logged in CABAL-DECISIONS.md)

- **D-001 through D-007**: Python 3.12, FastAPI, SQLAlchemy 2.0 async, Typer CLI, Authlib (joserfc).
- **D-008**: techne 0.6.0 installed via local-editable from `../infra-techne` until on PyPI.
- **D-009**: `events.id` is `Integer` not `BigInteger` (SQLite autoincrement quirk).
- **D-010**: Operator tokens transparently re-minted per-target in `/a2a/tasks`. Non-operator mismatched aud → 403.
- **D-011**: Circuit breaker lives at the LiteLLM client wrapper, not the proxy (§6.11). Single-probe HALF_OPEN.

## What's NOT built yet (Phase 2 outlook)

- **A2A v1.0 JSON-RPC envelope** (`message/send`, `tasks/get`, streaming via SSE). Current `/a2a/tasks` uses a Cabal-shaped request body. Refactor when we add a second agent that needs to call us programmatically.
- **Langfuse hook adapter** — the registry is ready; the LITELLM_POST handler that writes to Langfuse needs Langfuse client wiring + identity.toml.langfuse config.
- **`cbl primus rotate`** — schema supports it (composite PK + status column); CLI subcommand is a stub.
- **Budget reservation** (§6.8) — RESERVE_PER_DISPATCH = 50K tokens. Wrapper around LiteLLMClient that pre-checks budget.
- **session_token model** for long-running Tasks beyond 5-min JWT TTL. Schema and code both exist; resumption needs Phase 2 work.
- **Real personas** — Architect, Archivist, Librarian, Rook are not yet declared. `LLMPersona` is the base class they will subclass.
- **MCP gateway integration** — limenal connects via MCP; tool_use hooks fire from there once wired.
- **Docker compose + Litestream** — repo is `pip install -e .` development only. No Dockerfile / compose.yml yet.

## Suggested next session focus (Richard's call)

If you want to **demo the platform**: build one real persona (`Archivist` as a singleton with a system prompt for memory operations) using `LLMPersona`, declare it in `identity.toml`, watch it auto-register, dispatch through `cbl dispatch archivist "what do you remember?"`.

If you want to **harden the wire protocol**: refactor `/a2a/tasks` to accept A2A v1.0 JSON-RPC `message/send` envelopes and emit SSE for streaming — the spec contract the broker should honor when external agents start dispatching to us.

If you want to **observe what's running**: write the Langfuse handler that registers against LITELLM_POST + SESSION_START/END and exports traces. A few hours including config wiring.

Decisions on which path are yours; tonight's run made no choice between them.
