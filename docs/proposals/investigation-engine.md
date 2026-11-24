# Proposal: Investigation engine extensions — A2A spec streaming, multi-instance personas, sub-agent roles, mailbox

**Status**: Draft.
**Author**: Claude (Opus 4.7) + Richard, paired session 2026-06-13/14.
**Tracking issues**: [cabal#11](https://github.com/tenishai/cabal/issues/11) (AbortGraph), [cabal#12](https://github.com/tenishai/cabal/issues/12) (PR-A: A2A streaming types).
**Inspiration**: `incidentfox/incidentfox` (2026-06-13), `HolmesGPT/holmesgpt` (2026-06-14), `k8sgpt-ai/k8sgpt` (2026-06-14), `tensorzero/tensorzero` (2026-06-14). All architectural patterns logged in `architectural-patterns.md`.
**Prerequisite**: techne's outbox-sinks proposal (in the techne repo, not this one) — the durable, schema-validated outbox-sink substrate the new PR-E consumes. PR-E here is now a cabal-only consumer; the substrate work lives upstream in techne.

**Revisions**:
- 2026-06-15 (afternoon) — **Delivery substrate extracted to techne.** Earlier same-day revision built `deposit_finding` directly on the Archivist handler. After surveying `techne/sync/outbox.py`, the right home for the durable queue + sink registry + schema validation is techne (it's general substrate — future sinks like Slack-courier, email-out, tui-notifier register the same way). PR-E in this proposal is now the *consumer*: Archivist registers as a sink at startup, sub-agents enqueue via `techne.outbox`, the sink handler does A2A delivery to the Archivist agent, which runs receiver-side distill and publishes through the existing dedup pipeline. See `outbox-sinks.md` for the substrate design.
- 2026-06-15 (morning) — **Mailbox collapsed into the existing Archivist entry pipeline.** No separate `kind:mailbox-message` table. Sub-agents call a single dumb-producer tool with raw content; the Archivist runs a receiver-side distill (cheap model) and publishes through its existing dedup pipeline. "Mailbox" becomes a tag slice over the corpus, not a second-class storage layer. Findings *are* collective knowledge the moment they land. Detail in §Mailbox-as-tag-slice on Archivist.
- 2026-06-14 (final) — **major shrink after surveying actual cabal code.** Most of "the engine" (LLM loop, tool dispatch, memory integration, checkpoint, MCP integration, streaming SSE, persona registry, primus rotation, hot reload) already exists across 44 commits / 258 tests / 37 phases. This proposal now scopes only what's genuinely net-new: A2A spec streaming methods, multi-instance personas, sub-agent role library extension, and mailbox semantics over the Archivist.
- 2026-06-14 — Earlier drafts treated "the investigation engine" as a from-scratch build.

> **Companion proposal**: [`cabal-tui.md`](./cabal-tui.md) covers the multi-pane TUI that consumes the streaming + multi-instance work this proposal enables.

---

## What's already built (audit 2026-06-14)

A short tour of cabal's current state, citing actual file paths. This is the baseline; the proposal builds on it.

| Concern | Status | File |
|---|---|---|
| LLM client Protocol (vendor-agnostic) | **Built** | `cabal/core/llm/protocol.py` |
| LiteLLM client (library + proxy mode) | **Built** | `cabal/core/llm/litellm_client.py` |
| BudgetGuard (per-task token + cost) | **Built** | `cabal/core/llm/budget.py` |
| Circuit breaker (HALF_OPEN single-probe) | **Built** | `cabal/core/llm/circuit_breaker.py` |
| LLMPersona base class (the agent loop) | **Built** | `cabal/agents/llm_persona.py` |
| Subordinate sub-agent pattern (implementer/reviewer) | **Built** | `cabal/agents/subordinate.py` — *this is the existing sub-agent infrastructure* |
| MasonPrimusPersona (plan → dispatch → aggregate) | **Built** | `cabal/agents/personas/mason.py` |
| Architect, Archivist, Librarian | **Built** | `cabal/agents/personas/` |
| MCP tool proxy + per-persona tool surface | **Built** | `cabal/core/tools/mcp_proxy.py` |
| A2A types: AgentCard, Task, TaskStatus, Message | **Built** | `cabal/core/a2a/types.py` |
| A2A JSON-RPC (`message/send`, `tasks/get`) | **Built** | `cabal/core/a2a/jsonrpc.py` + `cabal/core/routes/a2a_jsonrpc.py` |
| AgentCard JWS signing + JWKS verification | **Built** | `cabal/core/a2a/card.py` |
| Bulletin board (agent registry, (name, generation) key) | **Built** | `cabal/core/bulletin/store.py` |
| Primus rotation (`cbl primus rotate`) | **Built** | `cabal/core/bulletin/store.py` + CLI |
| Dispatch broker (capability-based routing) | **Built** | `cabal/core/broker/router.py` |
| Persona factory (startup + hot reload) | **Built** | `cabal/core/persona_factory.py` |
| Cabal-native SSE streaming (`POST /agents/{name}/stream`) | **Built** (Phase S) | `cabal/agents/llm_persona.py::astream_task` |
| `cbl chat` consumes SSE | **Built** (Phase T) | `cabal/cli/main.py` |
| Event bus + admin SSE (`/admin/events/stream`) | **Built** (Phase U) | `cabal/core/observability/event_bus.py` |
| `cbl tail` / `cbl conversations` | **Built** (Phase U) | `cabal/cli/main.py` |
| Checkpoint store (cabal#2) | **Built** | `cabal/core/checkpoint.py` |
| Memory integration via techne | **Built** | `cabal/core/memory/techne_store.py` |
| Per-persona system prompt / model overrides | **Built** (Phase X) | `identity.toml` schema + `LLMPersona` |
| Hot reload of identity | **Built** (Phase Y) | `POST /admin/reload/identity` + `cbl reload identity` |
| Liminal MCP integration + tool_extras forwarding | **Built** (Phases Z/AA/BB) | `cabal/core/tools/MCPToolProxy` |
| Secrets table (encrypted at rest) | **Built** (Phase GG) | `cabal/core/secrets.py` |

The platform is much further along than the conversation context usually suggests. Most "investigation engine" concerns are solved or partially solved. This proposal targets only the gaps.

---

## What's net-new

Five additions, in dependency order:

1. **A2A spec streaming types** — `TaskStatusUpdateEvent`, `TaskArtifactUpdateEvent`, `MessageSendConfiguration`. Pure-additive type changes to `cabal/core/a2a/types.py`. Tracked at [cabal#12](https://github.com/tenishai/cabal/issues/12).
2. **`message/stream` + `tasks/resubscribe` endpoints** — A2A-spec-compliant streaming, distinct from cabal's existing non-spec `POST /agents/{name}/stream`. Cursor-based resubscribe (Last-Event-ID). The existing stream stays as-is for `cbl chat`; the spec stream is what external A2A clients (other operators' platforms, the TUI) will use.
3. **Multi-instance personas** — today one `mason-primus`. Net-new: `mason-primus`, `mason-secundus`, `mason-tertius` etc., all sharing the same persona type but with independent conversations + checkpoints + AgentCards.
4. **Sub-agent role library extension** — today only `implementer` + `reviewer` (Mason's subordinates). Net-new: `investigator`, `researcher`, `verifier`, `planner`, `documenter` as registered Subordinate roles any primus can spawn for a specific task.
5. **Mailbox-as-tag-slice on Archivist** — sub-agents need a durable place to drop findings when the spawner isn't listening, *and* those findings should land as collective knowledge, not as a second-class side table. Net-new: a `deposit_finding(addressee, sender, raw_content, parent_task)` tool that the Archivist receives, distills via its own cheap-model LLM (the producer stays dumb), and publishes through the existing dedup pipeline. "Mailbox" is a tag-filtered view of the corpus, not a separate kind.

---

## Three operator decisions still standing

These shaped the broader design and remain unchanged.

### Q1 — Hybrid MCP front door, Skills back of house

Operators reach personas over MCP via liminal (already shipping — Phases Z/AA/BB). When a persona does work (kubectl, observability, AWS, etc.), those are Skills (markdown + scripts loaded via Bash) — *not* yet built. The existing `MCPToolProxy` is the MCP adapter; a parallel SKILL.md adapter is part of the sub-agent role library extension (item 4).

### Q2 — LLM-agnostic via the existing LLMClient + LiteLLM proxy

Already shipping. `cabal/core/llm/protocol.py` is the Protocol; `LiteLLMClient` is the implementation. No vendor SDK imports anywhere.

### Q3 — Standard envelope, delivery varies by origin

Already partially shipping. `cabal/agents/llm_persona.py::astream_task` emits structured SSE events; `EventBus` aggregates fleet events. The PRs below add the A2A-spec envelope (`TaskStatusUpdateEvent` with `metadata.cabal_event`) alongside cabal's existing event shape. UIs binding to A2A-spec endpoints get the standard envelope; UIs binding to the existing SSE endpoints stay compatible.

---

## Sub-agent role library extension

The existing `Subordinate` pattern (`cabal/agents/subordinate.py`) is **the sub-agent model**. It's already:

- Spawned per task by a primus
- Owned by the primus (no AgentCard, not in the bulletin board)
- Configured by a role-derived system prompt from `SUBORDINATE_PROMPTS`
- Given its own Task ID derived from parent (`{parent_task.id}.{role}-{nonce}`)
- Subject to the same memory + session_tracker + checkpoint infrastructure as the primus

The extension is just **more roles**:

```python
# cabal/agents/subordinate.py — extend SUBORDINATE_PROMPTS dict
SUBORDINATE_PROMPTS = {
    "implementer": "...",  # existing
    "reviewer": "...",     # existing
    "investigator": (
        "You are an investigator subordinate. Given a question about the "
        "operator's system or data, gather evidence using the tools "
        "available, form a hypothesis, test it, and report what you found. "
        "Be specific. Distinguish 'this is the cause' from 'this is "
        "consistent with the cause.'"
    ),
    "researcher": (
        "You are a researcher subordinate. Given a topic, read the primary "
        "sources you have access to (substrate handlers, documents, code "
        "corpus), summarize the state of the question, and surface the "
        "open issues. Cite sources by file path or URL."
    ),
    "verifier": (
        "You are a verifier subordinate. Given a claim and the evidence "
        "supporting it, adversarially try to refute the claim. Find a "
        "counterexample, a stale assumption, a missing case. If you "
        "cannot refute it, say so directly."
    ),
    "planner": (
        "You are a planner subordinate. Given a goal and the current state, "
        "produce a short ordered list of steps to reach the goal. Each "
        "step should be concrete (a specific tool call or decision). Flag "
        "steps that depend on unknowns the planner can't resolve."
    ),
    "documenter": (
        "You are a documenter subordinate. Given some work that was done, "
        "write a clear summary suitable for the operator's records or for "
        "another agent picking up the thread later. Focus on what changed "
        "and why."
    ),
}
```

Identity.toml gains optional `[personas.<persona>.subordinates.<role>]` blocks beyond the existing `implementer` / `reviewer`. The persona_factory already builds Subordinates from this config; the change is one line in the iteration (line ~62 of `persona_factory.py`).

A persona that wants to spawn a sub-agent uses its existing `_dispatch_subordinate` method (in `primus.py`, on the shared `PrimusPersona` base). To make this available to any future primus persona, lift the dispatch helper from `primus.py` into a mixin on `LLMPersona`. ~30 lines.

---

## Multi-instance personas (stacks)

Today: one `mason-primus` in the bulletin board, addressable by `(name="mason-primus", generation=N)`. Two operator workflows want more.

**Workflow 1: Multiple conversational threads in parallel.** Richard is debugging staging while also planning a feature. Both need Mason-level orchestration. Today he runs two `cbl chat mason-primus` sessions which fight over the same conversation history.

**Workflow 2: Specialization within a persona type.** A `wright-secundus` configured for code-review work (different sub-agents, different toolset) running alongside `wright-primus` configured for general engineering. Same persona type; different specialization.

**The extension:**

- New dimension on identity.toml: `[personas.<persona>.instances]` block, defaulting to `["primus"]`. Multi-instance setup adds `["primus", "secundus", "tertius"]`.
- The persona_factory loop registers one bulletin-board entry per ordinal: `mason-primus`, `mason-secundus`, `mason-tertius`. Each has its own AgentCard (signed), own endpoint, own conversation table, own checkpoints.
- Per-instance overrides in identity.toml: `[personas.mason.instances.secundus]` can override `model`, `system_prompt_override`, `subordinates`, `commit_to_memory`, etc. Defaults inherit from the persona-type block.
- The `(name, generation)` key in the agents table becomes `(name, generation)` where name is the *instance* (`mason-secundus`). Primus rotation still works per-instance.
- Endpoint convention: `/agents/mason-primus`, `/agents/mason-secundus`, etc. A2A clients route by name.

The existing `MasonPrimusPersona` class is renamed (mentally — keep the class name for the rotation system that already exists) to `MasonInstance` semantically — the same code runs each instance.

Concrete additions:
- `cabal/config/identity.py` — parse `instances` list and per-instance overrides
- `cabal/core/persona_factory.py` — outer loop over instances per persona
- `cabal/cli/main.py` — `cbl personas add mason secundus` and `cbl personas remove mason secundus` admin commands
- Tests: hot-reload preserves per-instance state; spawning mason-tertius after startup works

---

## Mailbox-as-tag-slice on Archivist

When a sub-agent finishes work the spawner isn't actively watching, the finding needs (a) a durable place to live, and (b) a path to becoming team-consensus knowledge. Today: the result goes back as the Task's `result_message`; if nobody calls `tasks/get`, it sits in the agents table; if somebody reads it, it's a one-off blob with no curation.

**Earlier draft** introduced a separate `kind:mailbox-message` knowledge kind alongside `fact/decision/operation/error/procedure/document`. Retired 2026-06-15 — a separate kind splits the corpus, duplicates dedup logic, and forces a second "promotion" step before raw findings become collective knowledge.

**This shape**: the mailbox is a *tag-filtered view* of the existing entry corpus. A finding gets distilled on receive (the Archivist owns the LLM call, not the sub-agent) and published through the existing dedup pipeline. It IS collective knowledge the moment it lands.

### Producer contract (dumb on purpose, durable by default)

Sub-agents — including lightweight investigators that aren't strong summarizers — enqueue exactly one payload via the techne outbox:

```python
techne.outbox.enqueue(
    db_path=techne_db,
    target="archivist",                  # registered sink; UnknownSinkError otherwise
    data={
        "addressee": ["mason-primus"],    # one or many; tag-based fan-out
        "sender": "investigator",         # subordinate role or persona name
        "raw_content": "<freeform>",      # whatever the sub-agent produced
        "parent_task": "<task-id>",       # for cross-linking
        "hints": None,                    # optional pre-shaped fields; bypasses distill
    },
)
```

For ergonomics, cabal exposes this as a thin `archivist.deposit_finding(...)` skill on `LLMPersona` — same arguments, one-line shim over `techne.outbox.enqueue(target="archivist", ...)`. Producers can use either path; the skill is the conventional surface.

The producer is not expected to produce a well-shaped Entry. It hands over raw text. The contract is: *the Archivist will take it from here, durably.* If the Archivist is unreachable, the outbox row stays pending and retries on the next flush — the producer doesn't see the failure.

### Delivery: techne outbox → A2A → Archivist agent

The Archivist registers as the `"archivist"` sink at persona startup (see `outbox-sinks.md` for the substrate contract). Its sink handler is an A2A client call:

```python
def deliver_to_archivist(payload: dict) -> DeliveryResult:
    try:
        a2a_client.send_message(
            target="archivist",
            method="deposit_finding",
            params=payload,
        )
        return DeliveryResult(ok=True)
    except A2AError as e:
        return DeliveryResult(ok=False, error=str(e))
```

On the Archivist agent side, A2A `message/send` with method `deposit_finding` is a **typed protocol method** — it does NOT enter the LLM loop. It runs:

1. Distill via a cheap-model call (Haiku by default, configurable via `archivist.distill_model`) with a template prompt: "Extract a publishable Archivist entry from this finding. Return JSON: `{resource_name, summary, entry_type, tags, gotchas?}`."
2. Merge distilled fields with bookkeeping tags: `addressee:<name>` (one per recipient), `sender:<name>`, `parent_task:<id>`, `status:unread`, `source:sub-agent`.
3. Call existing `archivist.publish(...)` — dedup pipeline runs as it does today (L0 hash → L1 same-resource overlap → RELATED edge on near-overlap → INSERT or supersede).
4. Return `{entry_id, outcome: new | duplicate_l0 | duplicate_l1 | related}` as the A2A response.

The techne outbox row gets marked `synced` on `ok=True`, stays `pending` with the error on the row otherwise.

Distill latency: ~1–3 s on Haiku. Acceptable sync. If batch promotion is ever needed for cost or throughput, the existing `cabal/archivist/__init__.py` docstring already calls out a "background write-worker for async Tier-2 processing" as a deferred slot — that's where async distill would land.

If the producer wants to skip the LLM entirely (e.g. already-structured outputs from a deterministic tool), it can pass complete `hints` covering all required fields and the Archivist can bypass distill. Default path is distill-on.

### Mailbox view = tag slice

The "mailbox" of an addressee is just a search against the corpus:

```python
archivist.mailbox_list(addressee="mason-primus", status="unread")
# thin wrapper over:
archivist.search(tags=[f"addressee:mason-primus", "status:unread"])
```

`mark_read(entry_id)` / `mark_acted(entry_id, action_taken)` mutate the `status:` tag on the entry. The entry itself stays in the corpus — it never moves between tables, never gets "demoted", never expires unless an operator explicitly archives it.

### What this collapses

| Before (separate-kind draft) | After (tag-slice) |
|---|---|
| New `kind:mailbox-message` in techne `VALID_TYPES` | Nothing added to kinds — uses existing entry_types |
| Bespoke `mailbox.list/mark_read/mark_acted` storage | Tag mutation + thin search wrapper |
| Sub-agent expected to produce well-shaped content | Sub-agent emits raw; Archivist distills |
| Separate dedup logic for mailbox | Existing L0/L1 pipeline runs unchanged |
| Two-step promotion (mailbox → corpus) if you want findings as knowledge | One step — findings ARE corpus entries |
| Search is unified after promotion only | Search unified from day 1 |

### Concrete additions

Prerequisite (techne, separate PR per `outbox-sinks.md`): `OutboxSink` registry + tightened `enqueue` + multiplexed `flush_sinks`. None of the work below blocks on the substrate's *implementation details*, but it does block on the substrate's *API* landing first.

In cabal:

- `cabal/agents/personas/archivist.py` — at startup, `register_sink(OutboxSink(name="archivist", payload_schema=DepositFindingPayload, handler=deliver_to_archivist, ...))`. Wire a configurable `distill_model` (default `anthropic/claude-haiku-4-5-20251001`) onto the persona.
- `cabal/agents/personas/archivist.py` — new typed A2A protocol method `deposit_finding(params)` that bypasses the LLM loop and calls the distill+publish pipeline directly. Same shape as the existing `tasks/cancel` typed methods.
- `cabal/archivist/distill.py` (new file, ~40 lines) — `distill_raw_to_entry(raw, hints, llm_client, model) -> EntryFields`. Single template prompt; JSON-mode response; validates required fields. Falls back to `resource_name=raw-finding:<task-id>` + raw content in `details` on parse failure (no data loss).
- `cabal/handlers/archivist.py` — new tools `mailbox_list`, `mark_read`, `mark_acted` as thin tag-slice wrappers over `archivist.search` + tag mutation. *No* `deposit_finding` MCP tool — producers use `techne.outbox.enqueue` directly, or the `LLMPersona` skill below.
- `cabal/agents/llm_persona.py` — `deposit_finding(addressee, sender, raw_content, parent_task, hints?)` skill method, one-line wrapper over `techne.outbox.enqueue(target="archivist", data=...)`. Sub-agents inherit it.
- `cabal/agents/llm_persona.py::handle_task` — optional post-step: if the persona declares `deposit_findings_to: [<addressee>]` in identity, automatically call `deposit_finding` with the final assistant message at task completion. Opt-in per persona; defaults off.
- Tests:
  - sink registration happy path: Archivist starts → `list_sinks` shows `"archivist"`
  - end-to-end: `enqueue(target="archivist", ...)` → outbox row → sink handler → A2A → distill → publish → outbox row marked synced
  - distill happy path: raw text → valid Entry fields → publish returns `new`
  - distill with hints: producer-provided fields bypass LLM
  - dedup: depositing the "same" finding twice returns `duplicate_l0` on the second
  - mailbox isolation: two addressees, two unread counts, no cross-leak
  - mark_read flips tag and removes entry from unread search
  - distill failure: LLM returns invalid JSON → entry still publishes with sentinel `resource_name` (no data loss)
  - A2A failure (Archivist down): outbox row stays pending; subsequent `flush_sinks` succeeds and marks synced

### Why no separate kind survives

- Dedup, RELATED edges, supersession, FTS5 search, importance/decay, access counts — all already implemented for entries. Re-implementing them for a parallel mailbox table is pure duplication.
- A sub-agent reporting the same finding from two angles deduplicates **across the colony** automatically. With a separate mailbox kind, it wouldn't.
- The operator's "knowledge corpus" question — "what has the team learned about X?" — gets one answer, not two.

---

## Inter-Primus meta-channel

Today the EventBus broadcasts everything to every subscriber. For inter-Primus chatter (Mason-Primus negotiating with Wright-Primus about a hand-off), we want a *named channel* the operator can subscribe to without drinking from the firehose.

Net-new: a thin extension to `EventBus` for named channels.

```python
# cabal/core/observability/event_bus.py
@dataclass
class EventBus:
    _subscribers: dict[str, list[asyncio.Queue]] = field(default_factory=dict)

    def subscribe(self, channel: str = "default") -> asyncio.Queue: ...
    def publish(self, event: dict, channel: str = "default") -> None: ...
```

The default channel preserves existing behavior. The `"meta"` channel carries inter-Primus messages. A primus emits via a new skill — `talk_to(persona_name, message)` — which publishes to `meta` AND to that primus's A2A task (so the recipient's loop sees it as input on its next turn).

Concrete additions:
- `cabal/core/observability/event_bus.py` — channel parameter
- `cabal/core/routes/admin.py` — `/admin/events/stream?channel=meta` filter
- `cabal/agents/llm_persona.py` — `talk_to` skill (lift the dispatch helper from primus.py)
- Tests: subscribe-by-channel; multi-channel fan-out

---

## PR sequence

| PR | Scope | Estimate |
|---|---|---|
| **PR-A** — cabal#12 | A2A streaming types — pure-additive type changes in `cabal/core/a2a/types.py` + `jsonrpc.py` | small (~1 hr Claude-time) |
| **PR-B** | `message/stream` + `tasks/resubscribe` SSE endpoints with cursor-based replay. Reuses cabal's existing streaming infrastructure (`astream_task`) under the hood. | medium (~3-4 hr) |
| **PR-C** | Sub-agent role library extension — extend `SUBORDINATE_PROMPTS`; lift dispatch helper to `LLMPersona` mixin | small (~2 hr) |
| **PR-D** | Multi-instance personas — identity.toml schema + persona_factory loop + CLI admin commands | medium (~4-6 hr) |
| **PR-E** | Archivist registers as `"archivist"` outbox sink; typed `deposit_finding` A2A method runs distill + publish; `mailbox_list/mark_read/mark_acted` tag-slice wrappers; `LLMPersona.deposit_finding` skill wraps `techne.outbox.enqueue`. **Depends on techne sink registry PR landing first** (see `outbox-sinks.md`). | medium (~3-4 hr) |
| **PR-F** | Named-channel EventBus + `talk_to` skill + meta-channel admin route | small (~2 hr) |

PR-A and PR-B can land before any of the others. PR-C is independent. PR-D unblocks the multi-pane TUI. PR-E and PR-F together enable the meta-pane + mailbox pane in `cabal-tui.md`.

---

## Open design questions

### 1. Sub-agent retirement vs. pooling

After a sub-agent finishes its task and deposits a finding, does the Subordinate instance get retired immediately, or sit idle in a pool for future spawns of the same role?

**Recommendation**: retire immediately. The Subordinate is cheap to instantiate (no AgentCard, no bulletin board) and stateless beyond its task. Pooling adds complexity for no measurable gain at single-user scale.

### 2. Instance ordinal vs. arbitrary name

Latin ordinals (`primus`, `secundus`, `tertius`) match the existing `MasonPrimusPersona` naming. Should multi-instance always use ordinals, or allow named instances (`mason-bugs`, `mason-features`)?

**Recommendation**: ordinals only for V1. Named instances can come later if a real workflow needs them. Ordinals keep the bulletin-board key predictable.

### 3. Mailbox-addressee scope

Can a sub-agent address its finding to multiple recipients (e.g. `["mason-primus", "operator"]`)? Or one per deposit?

**Recommendation**: list of recipients. The Archivist entry carries N `addressee:<name>` tags. Subscribers query by their own name via `mailbox_list`. Cheap, expressive, no fan-out logic — and dedup still works because the entry is stored once.

### 4. AbortGraph integration (cabal#11)

When does the engine get AbortGraph for transitive cancellation? Today `tasks/cancel` only stops the immediate task; nested Subordinate calls don't propagate.

**Recommendation**: defer to its own PR. cabal#11 is unstarted. Ship the streaming + multi-instance work first; integrate AbortGraph as a follow-on once the cancellation primitive lands.

---

## Things deliberately NOT in scope

- **A separate "engine" module.** The engine is `LLMPersona` + `MCPToolProxy` + `Subordinate` + memory + checkpoint + BudgetGuard. Already there. Don't extract it into a new package.
- **A separate flavor table (Sherlock-V1, Pinkerton, Marlowe, Watson, Magnus).** The earlier proposal treated these as first-class persona types. They're not — they're sub-agent roles (item 4 above) or per-persona instances (item 3) with specialized toolsets. The Subordinate role library is the right grain.
- **K8s sandbox CRDs, Envoy sidecar credential injection.** Single-user platform; not needed.
- **Operator mode (24/7 background investigation).** HolmesGPT's pattern; out of scope for the personal platform.

---

## References

### architectural-patterns.md entries from this design session
- "Warm-sandbox claim/reclaim with TTL reset on follow-up" — incidentfox
- "Typed SSE event factories" — incidentfox
- "Schema-as-source-of-truth for tool risk classification" — Agent AFK
- "AbortGraph for transitive parent→child cancellation" — Agent AFK / cabal#11
- "Reference-based session handoff" — pi-setup
- "Intent-gated safety interruption" — pi-setup
- "Confidence-gated auto-resolution with human escalation queue" — incidentfox/self-learning-ai-agent
- "Intent-adaptive retrieval strategy fan-out" — incidentfox/self-learning-ai-agent
- "Per-toolset `llm_instructions` composed into the system prompt" — HolmesGPT
- "Spill-to-disk for oversized tool results + bash read-back" — HolmesGPT
- "JSON filter mixin: `jq` + `max_depth` as LLM-callable tool parameters" — HolmesGPT
- "Reversible anonymize-before-LLM / de-anonymize-after boundary" — k8sgpt
- "Per-flavor prompt templates registered alongside the flavor" — k8sgpt
- "Config-driven function/variant schema" — TensorZero
- "`episode_id` threading" — TensorZero
- "Bandit-in-the-gateway" — TensorZero

### Cloned sources
- incidentfox — https://github.com/incidentfox/incidentfox
- HolmesGPT — https://github.com/HolmesGPT/holmesgpt

### Companion proposals
- A techne outbox-sinks proposal (in the techne repo, not this one) — PR-E prerequisite. Substrate for typed, schema-validated, durable sink delivery in techne.
- `cabal/docs/proposals/cabal-tui.md` — UI consumer of PR-E's mailbox tag-slice tools.

