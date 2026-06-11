# Cabal Overnight Build Status — 2026-06-10/11

Autonomous-mode summary for morning review. See [CABAL-DECISIONS.md](CABAL-DECISIONS.md)
for the per-decision rationale log.

## Headline

- **GitHub**: https://github.com/dimartin/cabal — 26 commits, all on `main`.
- **Tests**: 177 passing, ~10s wall clock.
- **End-to-end operator UX works**:
  - `cbl daemon up` boots the platform (logs bootstrap code)
  - `cbl login --code <CODE>` mints an operator JWT
  - `cbl dispatch <persona> "..."` one-shot dispatch returns a Task
  - `cbl chat <persona>` multi-turn interactive mode with history
  - `cbl status` lists the agent fleet + heartbeats
  - `cbl primus rotate <persona>` rotates a primus generation
- **Four real personas**: Archivist (memory-backed via techne), Architect (opus, patterns), Librarian (citation-mandated docs), Rook (primus that fans out to implementer subordinate).
- **A2A v1.0 JSON-RPC**: `POST /agents/{name}` with `message/send` + `tasks/get`; per-agent `/.well-known/agent-card`.
- **Containerized**: Dockerfile + docker-compose.yml with LiteLLM proxy + Litestream sidecars.

## Build arc — what landed in each phase

| Phase | Topic | Tests | Highlight |
|---|---|---|---|
| 1 | scaffold | 5 | pyproject, Typer CLI shell, FastAPI smoke |
| 2 | db | +6 | SQLAlchemy 2.0 async + Alembic; composite-PK agents table |
| 3 | auth | +9 | RS256 keypairs + JWKS + master-key Fernet encryption |
| 4 | a2a card | +6 | JCS-canonicalized JWS AgentCards + verifier cache |
| 5 | bulletin+jwt | +10 | Rotation flow + JWT mint/verify with audience pinning |
| 6 | broker | +12 | DispatchRouter enforcing all §6.12 invariants (both modes) |
| 7 | e2e | +5 | Bootstrap-code → operator JWT → dispatch → echo |
| 8 | cli | +2 | `cbl login` + `cbl dispatch`; live-daemon CLI test |
| 9 | llm | +17 | LiteLLM wrapper + single-probe HALF_OPEN circuit breaker |
| 10 | codes+hooks | +9 | 700/701/702/704 reason codes + session_start/end firing |
| 11 | identity | +4 | identity.toml personas → bulletin board signed AgentCards |
| 12 | LLMPersona | +6 | LLM-backed LocalHandler base; system_prompt + history replay |
| 13 | ops | +6 | Heartbeat scheduler + `/admin/agents` + `cbl status` |
| F (14) | personas | +8 | Archivist + Architect + Librarian wired through identity.toml |
| G (15) | A2A JSON-RPC | +9 | `POST /agents/{name}` + `message/send`/`tasks/get` + result_json |
| H (16) | observability | +15 | TraceExporter + Langfuse adapter + BudgetGuard |
| I (17) | container | (docs) | Dockerfile + docker-compose with LiteLLM + Litestream |
| J (18) | techne memory | +7 | TechneMemoryStore wired into Archivist; recall on every dispatch |
| K (19) | Rook primus | +5 | PLAN → DISPATCH → AGGREGATE with subordinate fan-out |
| L (20) | primus rotate | +5 | `cbl primus rotate` + admin endpoint + schema-level rotation |
| M (21) | budget wired | +7 | BudgetGuard reserves/refunds per Task; 702 on exhaustion |
| N (22) | cbl chat | +5 | Multi-turn interactive chat with client-side history accumulation |
| O (23) | sibling techne | (docs) | `pip install -e ../techne` convention + Dockerfile additional_contexts |
| P (24) | rook reviewer | +4 | Rook now runs IMPLEMENTER → REVIEWER → AGGREGATE pipeline |
| Q (25) | session checkpoint | +9 | SessionTracker Protocol + TechneSessionTracker; per-Task techne sessions |
| R (26) | persona memory | +5 | Architect, Librarian, Rook + subordinates all opt into memory + session |
| S (27) | streaming | +8 | `astream`, `astream_task`, `POST /agents/{name}/stream` SSE endpoint |
| T (28) | streaming chat | +4 | `cbl chat` consumes SSE; prints deltas live; `--no-stream` fallback |
| U (29) | conversations + tail | +13 | EventBus pub/sub; `/admin/conversations`, `/admin/events/stream`; `cbl conversations`, `cbl tail` |
| V (30) | K8s manifests | (yaml) | `k8s/` with namespace + secrets template + ConfigMaps + PVCs + LiteLLM/cabal-core Deployments + Litestream sidecar + Kustomize base |
| W (31) | memory accumulation | +8 | LLMPersona opt-in `commit_to_memory`; Archivist defaults true; Q&A pairs flow into the techne knowledge corpus on every successful dispatch (sync + streaming) |
| X (32) | persona overrides | +5 | `system_prompt_override`, `model_override`, `max_tokens_override`, `commit_to_memory` per persona in identity.toml — operator tunes behavior without editing Python |
| Y (33) | identity hot reload | +5 | `POST /admin/reload/identity` + `cbl reload identity`; rebuilds persona handlers without restarting; LiteLLMClient (breaker + budget state) preserved |

**Total: 237 tests, 40 commits.**

## What the morning operator can demo

### Local (Python venv) — no LLM required (echo agent)

```bash
cd cabal
source .venv/bin/activate
export CABAL_MASTER_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
cbl daemon up

# Other terminal
cbl login --code <CODE_FROM_DAEMON_LOG>
cbl dispatch echo "hello"
cbl status
```

### Docker compose — full stack with LiteLLM

```bash
cp .env.example .env                       # CABAL_MASTER_KEY, LITELLM_MASTER_KEY, ANTHROPIC_API_KEY
cp identity.toml.example identity.toml     # archivist + architect + librarian + rook all declared
docker compose build && docker compose up
docker compose logs cabal-core | grep bootstrap_code
cbl login --code <CODE>
cbl chat archivist                         # interactive memory-backed session
cbl dispatch rook "write me a Python http server"   # plan + implement + aggregate
cbl primus rotate rook                     # ratchet rook to generation 2
```

## §6.12 Dispatch Router invariants — implementation status

| Invariant | File | Tested? |
|---|---|---|
| 1. AgentCard signature verify + cache + kid invalidation | `core/a2a/card.py` | ✓ 5 tests |
| 2. Caller JWT mint+verify on every dispatch (in-process too) | `core/auth/jwt.py` + `core/broker/router.py::_prepare` | ✓ 3 tests across modes |
| 3. Broker bookkeeping participation, both modes | `core/broker/bookkeeping.py` + `router._dispatch_*` | ✓ 2 dedicated tests + parity test |
| 4. Trace context propagation via `task.metadata.cabal_trace_id` | `router._ensure_trace` | ✓ generation + preservation tests |

## Decisions made tonight (logged in CABAL-DECISIONS.md)

- **D-001–D-007**: Python 3.12, FastAPI, SQLAlchemy 2.0 async, Typer CLI, Authlib (joserfc).
- **D-008**: techne 0.6.0 installed via local-editable from `../infra-techne` until on PyPI.
- **D-009**: `events.id` Integer not BigInteger (SQLite autoincrement quirk).
- **D-010**: Operator tokens transparently re-minted per-target in `/a2a/tasks`. Non-operator mismatched aud → 403.
- **D-011**: Circuit breaker at the LiteLLM client wrapper, not the proxy. Single-probe HALF_OPEN.
- **D-012**: Docker image skips techne until 0.6.0 is published to a real index.
- **D-013**: Container CMD goes through `cbl daemon up`, not raw uvicorn.
- **D-014**: TraceExporter defaults to async — observability never blocks dispatch.

## What's NOT built yet

- **A2A streaming**: `message/stream` + `tasks/resubscribe` (SSE).
- **techne run_session_start/end integration**: Personas don't yet checkpoint Task state to techne sessions; only recall is wired.
- **MCP gateway (limenal)**: tool_use hooks fire from the MCP path once wired.
- **K8s manifests**: only docker-compose for now.
- **Operator dashboard UI**: web frontend that consumes `/admin/agents` + a future `/admin/events` SSE.
- **session_token model**: long-running Tasks past the 5-min JWT TTL.
- **GraphQL / Slack adapter**: still A2A-only on the wire.
- **Rook with reviewer subordinate**: identity.toml declares it, the persona currently only uses `implementer`.

## Pre-public-release checklist (must clear before this repo goes public)

The current build assumes a sibling `../techne` checkout. That is fine for solo
local + LAN deployment but is a blocker for going public. Before flipping the
repo public:

- [ ] **Publish techne to PyPI** (or any pip-installable index). Will need a
      PyPI account, project-name reservation, a CI publish workflow
      (`twine upload` from a trusted runner), and a first stable release tag.
- [ ] In `cabal/pyproject.toml`, add `techne>=0.6.0` to `dependencies`.
- [ ] In `cabal/Dockerfile`, drop the `COPY --from=techne` + `pip install /build/techne`
      lines (`pip install .` then pulls techne from PyPI on its own).
- [ ] In `cabal/docker-compose.yml`, remove the `additional_contexts.techne` block.
- [ ] In `cabal/README.md`, drop the "clone techne as a sibling" prerequisite
      and the `pip install -e ../techne` step.
- [ ] In `cabal/CABAL-DECISIONS.md`, mark D-008 and D-012 SUPERSEDED with a
      pointer to a new decision entry recording the PyPI flip.
- [ ] Decide whether Cabal itself should be on PyPI by then so
      `pip install cabal` works for adopters.

## Suggested next session focus

1. **Make Rook use reviewer**: tweak Rook's pipeline to do implementer → reviewer → aggregate, and ship a one-line identity.toml change to demonstrate.
2. **A2A streaming**: build `message/stream` SSE on top of the existing per-agent JSON-RPC route. Required for any UI that wants live token-by-token output.
3. **techne session checkpoint**: wire `init_session_for_external_id` + `run_session_end` into LLMPersona so personas remember per-Task state, not just static knowledge.

Decisions on which path are yours; tonight's run made no choice between them.
