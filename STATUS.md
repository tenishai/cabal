# Cabal Overnight Build Status — 2026-06-10/11

Autonomous-mode summary for morning review. See [CABAL-DECISIONS.md](CABAL-DECISIONS.md)
for the per-decision rationale log.

## Headline

- **GitHub**: https://github.com/dimartin/cabal — 19 commits, all on `main`.
- **Tests**: 147 passing, ~9s wall clock.
- **End-to-end loop works**: `cbl daemon up` → `cbl login` → `cbl dispatch echo …` returns a real Task with bookkeeping rows, signed JWT, session_start/end hooks fired, heartbeat ticking. Also dispatches against `cbl dispatch archivist …` (or architect / librarian) once identity.toml is in place — handler calls LiteLLM (stubbed in tests, real in compose stack).
- **Phase 1 (per cabal-handoff.md §Phase 1 Build Order)**: complete.
- **Phase 2 foundation**: LiteLLM client + breaker + LLMPersona; identity.toml-driven persona registration; A2A v1.0 JSON-RPC envelope at /agents/{name}; trace exporter (Langfuse adapter); budget guard; heartbeat scheduler.
- **Containerized**: Dockerfile + docker-compose.yml + Litestream + LiteLLM proxy compose service.

## What landed (in build order)

| # | Phase | Commit topic | What it ships |
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
| 14 | personas | Archivist + Architect + Librarian | Three real personas registered from identity.toml, dispatch through LLM |
| 15 | a2a | JSON-RPC 2.0 envelopes | `POST /agents/{name}` per A2A v1.0; message/send + tasks/get; result_json on conversations |
| 16 | observability | TraceExporter + Langfuse | Lifecycle hooks → TraceClient; LangfuseTraceClient (lazy import) + RecordingTraceClient for tests |
| 17 | llm | BudgetGuard (§6.8) | Per-task reserve/refund; default 200K budget, 50K reservation; BudgetExhaustedError |
| 18 | container | Dockerfile + compose | Multi-stage build; non-root user; volume for /data; compose includes LiteLLM + Litestream sidecars |

## What the morning operator can demo

### Local (Python venv)

```bash
cd cabal
source .venv/bin/activate
export CABAL_MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
cp identity.toml.example identity.toml   # already has archivist/architect/librarian
cbl daemon up                            # → boots, logs bootstrap code

# In another terminal
cbl login --code <CODE_FROM_LOG>
cbl dispatch echo "hello cabal"          # → "[completed] echo: hello cabal"
cbl status                               # → table: echo, archivist, architect, librarian, rook-primus
```

Personas only LLM-respond if a LiteLLM proxy is reachable at `http://litellm:4000`
(or whatever you set in identity.toml.litellm.api_base). Local dev without LiteLLM:
edit identity.toml and remove the [personas.*] blocks, or override api_base.

### Docker compose

```bash
cp .env.example .env                     # fill in CABAL_MASTER_KEY, LITELLM_MASTER_KEY, ANTHROPIC_API_KEY
cp identity.toml.example identity.toml
docker compose build
docker compose up
docker compose logs cabal-core | grep bootstrap_code
cbl login --code <CODE>                  # from host (127.0.0.1:7400 published)
cbl dispatch archivist "what do you remember from earlier?"
```

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
- **D-012**: Docker image deliberately skips techne until 0.6.0 is published.
- **D-013**: Container CMD goes through `cbl daemon up`, not raw uvicorn.
- **D-014**: TraceExporter defaults to async — observability never blocks dispatch.

## What's NOT built yet

- **Streaming**: A2A v1.0 `message/stream` + `tasks/resubscribe` (SSE). The JSON-RPC envelope handles request/response; streaming lands when an actual streaming consumer needs it.
- **`cbl primus rotate`**: schema supports it (composite PK + status column); CLI subcommand is a stub.
- **techne integration in personas**: LLMPersona checkpoint/resume via `init_session_for_external_id` is the next big architectural lever; needs identity.toml.techne block + tests.
- **Real Rook primus handler**: identity.toml declares `[personas.rook]` role=primus; bulletin row registers as `rook-primus` but no LocalHandler exists. Phase 2 work.
- **MCP gateway integration** (limenal): tool_use hooks fire from there once wired.
- **K8s manifests**: only docker-compose for now. K8s when we want to dispatch persona containers per-pod.

## Suggested next session focus

If you want to **make personas remember between dispatches**: wire techne into LLMPersona's `handle_task` — call `init_session_for_external_id(config, task.id)`, replay session state, persist on return. That's the moment Cabal stops being a thin LLM proxy and becomes a memory system.

If you want to **demo with another agent calling us**: write a tiny client that posts a `message/send` JSON-RPC envelope to `/agents/archivist` from outside the Python process — proves the wire protocol is honest.

If you want to **deploy**: `docker compose up` with real env values + verify Langfuse traces show up in your project.

Decisions on which path are yours; tonight's run made no choice between them.
