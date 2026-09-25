# Cabal — System Architecture

One diagram, one read. For the decisions behind it see [CABAL-DECISIONS.md](../CABAL-DECISIONS.md);
for the build log see [STATUS.md](../STATUS.md) and [CABAL-DECISIONS.md](../CABAL-DECISIONS.md).

```mermaid
graph TB
    %% ===== Clients =====
    CLI["cbl CLI<br/>login · dispatch · chat · tail"]
    UI["TUI / future web UI"]

    %% ===== Cabal Core =====
    subgraph Core["Cabal Core — FastAPI singleton"]
        direction TB
        IDP["IdP<br/>JWT mint+verify · Fernet secrets"]
        BROK["Dispatch Router<br/>(local-call ↔ HTTP)"]
        BB[("Bulletin Board<br/>signed AgentCards")]
        EB["Event Bus (SSE)"]
        PROJ["Projects · Conversations<br/>Events · Notifications"]
    end
    DB[("SQLite cabal.db<br/>FTS5 · WAL")]
    LITS["Litestream sidecar<br/>→ S3 / GCS / sftp"]

    %% ===== A2A wire =====
    A2A{{"A2A v1.0 JSON-RPC<br/>message/send · tasks/get · stream"}}

    %% ===== Personas =====
    subgraph Project["Project Personas"]
        direction TB
        MASON["mason-primus<br/>(lead · opus)"]
        MASONSUB["mason-implementer<br/>(worker · sonnet)"]
        RAVEN["raven-primus"]
    end

    subgraph Single["Singleton Personas (front data stores)"]
        direction TB
        ARCH["Architect — patterns"]
        ARCHV["Archivist (v2) — federated memory"]
        LIB["Librarian (v2) — docs"]
    end

    %% ===== Per-agent substrate =====
    subgraph PerAgent["Per-agent substrate (inside every persona)"]
        direction TB
        LIM["liminal<br/>MCP gateway + tool scoping"]
        TEC[("techne<br/>memory + session checkpoints")]
        LLMCLI["LiteLLM client<br/>circuit breaker + budget"]
    end

    %% ===== External =====
    LITE["LiteLLM proxy<br/>routing · per-key spend"]
    PROV(("LLM providers<br/>Anthropic · OpenAI · ..."))

    %% ----- Wires -----
    CLI --> IDP
    CLI --> BROK
    UI --> IDP
    UI --> BROK

    BROK <--> BB
    BROK --> A2A
    BROK <--> EB
    BB --- DB
    PROJ --- DB
    EB --- DB
    IDP --- DB
    DB <-. WAL replicate .-> LITS

    A2A --> MASON
    A2A --> RAVEN
    A2A --> ARCH
    A2A --> ARCHV
    A2A --> LIB

    MASON -. subordinate fan-out .-> MASONSUB

    %% every persona wires through the per-agent substrate
    MASON & RAVEN & MASONSUB & ARCH & ARCHV & LIB --> LIM
    MASON & RAVEN & MASONSUB & ARCH & ARCHV & LIB --> TEC
    MASON & RAVEN & MASONSUB & ARCH & ARCHV & LIB --> LLMCLI

    %% MCP shallow retrieval bypasses A2A for the singleton data backends
    LIM -. shallow MCP .-> ARCH
    LIM -. shallow MCP .-> ARCHV
    LIM -. shallow MCP .-> LIB

    LLMCLI --> LITE
    LITE --> PROV

    %% lifecycle hooks feed the bus
    MASON -. lifecycle hooks .-> EB
    RAVEN -. lifecycle hooks .-> EB
    ARCH -. lifecycle hooks .-> EB

    %% styling
    classDef core fill:#1f2937,stroke:#374151,color:#f9fafb
    classDef persona fill:#1e3a8a,stroke:#1e40af,color:#f9fafb
    classDef singleton fill:#3730a3,stroke:#4338ca,color:#f9fafb
    classDef sub fill:#065f46,stroke:#047857,color:#f9fafb
    classDef ext fill:#7c2d12,stroke:#9a3412,color:#f9fafb
    classDef wire fill:#facc15,stroke:#ca8a04,color:#1f2937
    class IDP,BROK,BB,EB,PROJ core
    class MASON,MASONSUB,RAVEN persona
    class ARCH,ARCHV,LIB singleton
    class LIM,TEC,LLMCLI sub
    class LITE,PROV,LITS ext
    class A2A wire
```

## Layers

**Clients.** `cbl` is the operator-facing CLI; everything in the diagram is reachable
through it. Future TUI / web UIs land on the same auth + dispatch endpoints.

**Cabal Core.** One FastAPI singleton holding the dispatch router, bulletin board
(signed AgentCards keyed by name), JWT IdP (issues operator + per-persona bearer
tokens; rotates the AgentCard signing key under Fernet at rest), project/conversation/
event tables, and the SSE event bus. State lives in one SQLite file; Litestream
streams the WAL off-host for durability.

**A2A v1.0 wire.** Every dispatch — operator-to-persona and persona-to-persona —
is an A2A `message/send`. The router collapses local hops to direct Python calls
when target and caller share a process. The caller's bearer JWT is verified on
every hop regardless (`router._prepare`), so the security model is uniform across
solo and per-pod deployments. The router does **not** verify AgentCards when it
dispatches: `_dispatch_remote` forwards the caller's JWT, and that JWT is the
trust. `AgentCardVerifier` (`core/a2a/card.py`) exists and is tested, but nothing
on the dispatch path calls it (#226).

**Personas.** Project personas (mason, raven, …) own the work. Primus agents own
plan/dispatch/aggregate; subordinates fan out under them. Singleton personas
(Architect; Archivist + Librarian in v2) front shared data stores and expose two
faces: A2A for synthesis dispatches and MCP for cheap shallow retrieval that
bypasses the LLM.

**Per-agent substrate.** Every persona container (and the solo-mode in-process
equivalent) ships the same three pieces: **liminal** as the MCP gateway and
tool-scoping runtime, **techne** as agent-local memory + session checkpoints,
and the **LiteLLM client** wrapper with a single-probe circuit breaker and
budget reservation. Identical inside project personas and singleton personas.

**External.** LiteLLM proxy (one per deployment) handles model routing and
per-virtual-key spend. Litestream pushes WAL frames to object storage.

## Conventions

- **Solid arrow** — request/dispatch flow (caller → callee).
- **Dotted arrow** — async, event-emitted, or out-of-band (lifecycle hooks,
  subordinate fan-out, shallow MCP retrieval, WAL replication).
- **Diamond node (A2A)** — protocol surface, not a process.
- **Hexagon nodes (techne, DB, bulletin board)** — durable stores.

## What is *not* in this diagram

- Notification adapters (Slack, iMessage, …) hang off `Notifications` table and the
  event bus; they're transport-shaped, not architectural.
- Hot-reload (`POST /admin/reload/identity`) — operational surface, not data flow.
- Streaming SSE (`message/stream`) — same broker path with token-by-token framing
  on top.
- Detailed lifecycle hook taxonomy — NOT WRITTEN DOWN. `HookName` at
  `src/cabal/core/hooks/registry.py:37` is the authority; the call sites are the taxonomy.
  (This line used to point at a `docs/cabal.md` that does not exist in this
  repo — see #202.)
