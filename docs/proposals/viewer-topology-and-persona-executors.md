# Proposal: Claude Code as viewer, cabal as dispatch surface, pluggable persona executors

> ## ⚠️ SUPERSEDED, same day, by
> ## `operator-fleet-design.md`, in the agents repo (not public; summary below)
>
> Two errors this draft makes, both corrected there:
>
> 1. **Wrong unit.** It frames workers as cabal *personas*. They are **agent
>    homes** — full Claude Code runtimes with their own config dir and their own
>    accumulating memory. A static persona cannot hold the organic growth of a
>    session, which is the entire point.
> 2. **Wrong scope.** It scopes in multi-viewer (Zed/Neovim/mobile) and
>    multi-operator. Both were ruled out — one viewer, one operator.
>
> **Kept for**: §6 (Anthropic auth/licensing, verified) and §7 (ACP protocol
> shape, cost asymmetry, adapter landscape). Both are cited from the successor
> rather than duplicated. Do not build from this document.

**Status**: superseded — see above
**Date**: 2026-08-02
**Author**: session notes (Richard + Poe)

Reference notation: `gh:cabal#N` / `gh:techne#N` = GitHub issue in that repo.
`kn:N` = entry in the personal techne knowledge store. Bare `techne#N` is
deliberately avoided — it denotes three different namespaces depending on where
it is written (see `kn:929`).

---

## 1. The problem

The operator is currently the router. Work is split across named Claude Code
sessions in tmux tabs (`poe_1`, `poe_2`), and moving work between them is a
human context switch. Naming them is manual; read state, task state, and
attention all live in the operator's head.

The name is load-bearing in three places today, which is why it can't just be
auto-generated and forgotten:

- `resolve_recipient()` (`techne/core/coord.py:50`) — resolves a display name to
  a session_id for direct messages; a name matching no live session **raises**.
- `from_name` is snapshotted permanently into `coord_messages` at send time.
- the session-start banner peer list.

`identity` in techne is a plain key/value config table — **not** keyed on session
name, so it is not involved.

## 2. The reframe

"Poe" is one operator's name for their agent, not a persona type — a second
operator names their own. The slot is per-operator identity, filled
dynamically — a multiplex, not a fixed roster.

The topology inverts from the obvious one:

```
  operator ──► Claude Code (viewer) ──► Poe (primus) ──[MCP]──► cabal ──► fleet
```

Claude Code is the viewing surface. The in-session agent is the primus. Cabal is
a **tool surface** the primus dispatches into — not an orchestrator of Claude
Code sessions.

### Why this dissolves an older question

A June design conversation asked whether cabal should be a "meeting space" and
concluded A2A is dispatch-shaped, not conversation-shaped: one caller, one
target, one Task, no rooms, no broadcast. The lean at the time was to fold
rookery's mailbox into cabal to fix it.

In this topology none of that is needed. There is exactly one conversational
surface — the viewer — and cabal stays dispatch-shaped, which is what it already
is and is good at. **The room abstraction was only necessary while the operator
was the router.**

## 3. What already exists (verified against source, 2026-08-02)

**limenal is the MCP gateway.** "One MCP connection per agent, many backend
handlers behind it." 198 tests.

**cabal already registers handlers into limenal** via an entry-point group
(`cabal/pyproject.toml`):

```toml
[project.entry-points."limenal.handlers"]
patterns  = "cabal.handlers.patterns:get_handler"
documents = "cabal.handlers.documents:get_handler"
code      = "cabal.handlers.code:get_handler"
archivist = "cabal.handlers.archivist:get_handler"
```

So `Claude Code → MCP → limenal → cabal substrate` is **already built**.

**cabal's dispatch machinery is complete**: bulletin board, DispatchRouter with
§6.12 invariants (`core/broker/router.py`), per-agent JSON-RPC at
`POST /agents/{name}` with `message/send` + `tasks/get` + SSE streaming
(`core/routes/a2a_jsonrpc.py`), EventBus, capability registry, circuit breaker,
budget guard.

**Primus fan-out exists**: mason and wright are `role = "primus"` with
subordinates; the shape is PLAN → dispatch → AGGREGATE.

## 4. The gaps

### 4.1 No dispatch handler — the whole gap, in one line

All four registered handlers are **read/substrate**. None exposes dispatch. A
viewer can search the pattern corpus and query the code graph but **cannot
command the fleet**.

The missing piece is one handler exposing `message/send` + `tasks/get` (and
probably an EventBus tail) through the `limenal.handlers` group. That is the
entire mechanical delta for the topology in §2.

**Loop hazard**: personas keep a `tool_proxy` to limenal. If limenal also
exposes dispatch, a persona can dispatch to itself. Dispatch scope must be
grantable to the viewer/operator token and denied to persona tokens. limenal's
`cabal_scope` enforcement already supports this; it has to be deliberate.

### 4.2 Single operator — the multiplex bottleneck

`operator_id` is a scalar (`config/identity.py:52`), and auth mints
`sub = f"operator:{operator_id}"` (`core/routes/auth.py:75`). **One operator per
cabal instance.**

Fine for one operator with many agents. Not sufficient for several operators
each with their own named agent against a shared cabal. That is a real change to
cabal's auth model, not a config tweak.

Note the multiplex *already exists one layer up*: limenal is per-agent by
construction — one connection per agent, `aud` + `cabal_scope` enforced per call,
and `cabal_tool_extras` clamping where operator config is the ceiling and a JWT
can narrow but never widen.

### 4.3 Worker quality is a separate problem

Personas are `LLMPersona` → LiteLLM. Tools arrive only via optional `tool_proxy`,
capped at `tool_use_max_steps: int = 4`. The live `identity.toml` disables MCP
entirely ("MCP gateway (Tier 1) stays off") and points every persona at a LAN
Ollama box running `ornith:9b`.

So dispatch today reaches a 9B local model with no tools. For substrate work
(patterns, memory, docs, code graph) that is fine — those handlers barely need a
model. For engineering work it is not.

In-code defaults are also stale: `claude-opus-4-7` (mason, architect),
`claude-sonnet-4-6` (librarian, archivist), `claude-sonnet-5` (wright).

## 5. Persona executors

The proposal: **executor is a per-persona config choice**, not a global.

| Executor | Auth | Good for | Real limit |
|---|---|---|---|
| **API persona** — LiteLLM → Anthropic / Ollama / other | API key | async, parallel, headless fleet work | costs API money; this is what exists today |
| **Borrowed inference** — emit/ingest | none | judgment, synthesis, review | **serial, and only while a session is live** |
| **ACP agent** — speak ACP to Claude Code, Goose, or any ACP agent | that agent's own | full harness: shell, edit, hooks, skills | see §6; adapter maturity unverified |

### 5.1 Borrowed inference, and why it is not a credential dodge

techne already ships this pattern (`techne/cli.py:1902`):

> "Emit per-thread reflection prompts for an agent to answer (**no API key**).
> techne renders the prompts, **an in-session agent (on the operator's OAuth
> model)** answers each as a JSON candidate array, and `techne reflect ingest`
> feeds them back."

Used by `reflect`, `dream`, `consolidate`, `faithfulness`, `memory_claims`.

The direction is what makes it legitimate: **techne never authenticates to
Anthropic.** It renders a prompt; the in-session agent answers as the ordinary
first-party client doing ordinary interactive work; the result is ingested. No
token leaves Claude Code, and no third-party app presents operator credentials.
Work is handed *inward* to a session that already has a model, not *outward*
with borrowed keys.

**Its structural limit is the binding constraint on this architecture**:
borrowed inference has **no daemon**. There is no in-session agent for cabal to
dispatch into between operator messages. It works as synchronous delegation
inside a turn the agent is already taking — exactly how techne uses it — and not
at all as a background fleet.

**Consequence**: the fleet runs on API keys for anything asynchronous or
parallel; borrowed inference is reserved for judgment steps the viewer is
already awake for. This is also the cheapest arrangement.

## 6. Authentication and licensing (HIGH confidence, verified 2026-08-02)

- **Feb 2026**: Anthropic added an explicit Authentication and Credential Use
  policy — OAuth from Free/Pro/Max is intended **exclusively for Claude Code and
  claude.ai**, not third-party products and **not the Agent SDK**.
- January's block was reversed after backlash; **April 4, 2026** an outright ban
  on third-party agents using subscription credentials was enforced.
- Integrations, Agent SDK included, must use **API key auth** from Claude Console
  or a supported cloud provider.
- **MEDIUM / contested**: the June 15 2026 move of automated workflows off the
  subscription pool onto a credit pool. Current sources say it took effect; our
  own June research captured a headline saying Anthropic *paused* it.
  **Re-verify before costing anything.**

### 6.1 The Goose ACP case — unresolved, deliberately

Goose's `claude-acp` provider "wraps `claude-agent-acp`, an ACP adapter for
Anthropic's Claude Code. Uses the same Claude subscription as the deprecated
`claude-code` CLI provider," advertising "no per-token API costs."

Mechanically this is **not credential passthrough** — Goose never holds the
token; it drives the Claude Code CLI, which authenticates as itself. Structurally
it is the subprocess approach with a standard wire format.

Whether Anthropic considers that permitted is **not established**. The client
genuinely is Claude Code, but the economic behavior — a third-party harness
consuming subscription inference at no per-token cost — is what the policy exists
to prevent, and the docs' own note that the direct `claude-code` provider is
"deprecated" reads like that pressure landing. **Operator's call; do not assert
it is fine.**

## 7. ACP as the shape (the part orthogonal to §6)

ACP = **Agent Client Protocol** — client-to-agent, *not* agent-to-agent.
JSON-RPC 2.0, deliberately LSP-shaped. The agent runs as a **subprocess of the
client**.

```
initialize            version + capability negotiation
authenticate          if required
session/new | load    a conversation with accumulating context
session/prompt        one turn
  ← session/update              streaming progress notifications
  ← session/request_permission  agent asks, CLIENT decides
  ← fs/read_text_file, terminal/*  agent asks, CLIENT executes
session/cancel        interrupt
```

### 7.1 The resource inversion — the part that matters most here

**The agent does not own the filesystem or the terminal.** The spec puts that on
the client: clients "manage the environment, handle user interactions, and
control access to resources." The agent calls `fs/read_text_file`,
`terminal/create`, `session/request_permission` and waits. All capability-gated;
all paths absolute; line numbers 1-based.

This is the opposite of shelling out to `launch.sh -p`, where a subprocess gets
the whole machine on trust. And cabal already has the matching machinery —
`tool_call_guardrails` / `tool_result_guardrails` on `LLMPersona`, applied before
and after dispatch — which maps directly onto `session/request_permission`.

### 7.2 ACP and MCP stack; they do not compete

**MCP is inside the agent. ACP is outside it.**

```
operator
 └── viewer            ACP CLIENT — owns env, gates permissions
      │                (Claude Code, Zed, Neovim, cabal's TUI)
      │  ── ACP ──
      └── agent        ACP AGENT — subprocess, requests resources
           │           (claude-agent-acp, goose, a cabal persona)
           │  ── MCP ──
           └── limenal gateway → handlers
                 patterns · documents · code · archivist · [dispatch]
```

Confirmed empirically twice: Goose passes its extensions through to the ACP agent
**as MCP servers**, and `claude-agent-acp` lists "Client MCP servers" among its
supported features. **limenal does not move or change.**

### 7.3 The implementation-cost asymmetry (inverts the obvious guess)

From the v1 schema, the two directions are not remotely the same size:

**cabal as ACP AGENT — small.** A minimal conformant agent implements
`initialize`, `session/new`, `session/prompt`, `authenticate`, `logout`, and
emits `session/update` notifications. Everything else is capability-gated
(`session/load`, `resume`, `list`, `delete`, `close`, `set_mode`, …) and can be
declined at negotiation. cabal already has sessions (the `Conversation` model),
streaming (SSE + EventBus) and dispatch — the mapping is close to mechanical.

**cabal as ACP CLIENT — large.** The client must implement the whole
agent-to-client surface: `fs/read_text_file`, `fs/write_text_file`,
`terminal/create` + `output` + `wait_for_exit` + `kill` + `release`,
`session/request_permission`, `elicitation/create`. That is a terminal subsystem
and a permission UI, not an afternoon.

**So the cheap direction is the one that delivers the multiplex, and the
expensive direction is the one that delivers executors.** That is the reverse of
the intuitive guess ("just be a client and drive Claude Code"), and it should
drive sequencing.

### 7.4 Two implications

1. **Executor row 3 becomes "speak ACP to any agent"** rather than a bespoke
   subprocess bridge. Claude Code is one such agent; Goose is another. Someone
   else maintains the adapter. Cost: §7.3's large side.
2. **If cabal implements the ACP agent side, the viewer stops being singular.**
   Zed, Neovim and mobile join Claude Code as viewers, each authenticating as its
   own client. That is the §2 multiplex, delivered at the client layer, on
   §7.3's small side. It does **not** remove the §4.2 `operator_id` work —
   separate operators still need distinct identities in cabal core — but it
   decouples "who is looking" from "who is authorized."

### 7.5 Adapter landscape — the fork follows the licensing line

**Official**: `agentclientprotocol/claude-agent-acp` — Apache-2.0, no CLA, 2330
stars, **pushed 2026-08-02** (same day as this doc), not archived. npm
`@agentclientprotocol/claude-agent-acp` **v0.64.0**. Renamed from
`@zed-industries/claude-code-acp`; the old `@zed-industries/*` package is ~2
months stale. README, verbatim: *"This tool implements an ACP agent by using the
official Claude Agent SDK."* Surfaces tool calls with permission requests,
interactive + background terminals, client MCP servers, nested subagent
transcripts, custom slash commands.

**Third-party CLI bridges**, which exist specifically to drive the CLI:
`harukitosa/claude-code-acp` ("use your Pro/Max subscription from any
ACP-compatible editor"), `moabualruz/claude-code-cli-acp` (Rust PTY bridge to
"the real Claude Code CLI"), `Xuanwo/acp-claude-code`.

**The fork maps exactly onto §6**: official → Agent SDK → API key → sanctioned;
third-party bridges → drive the CLI → subscription → the zone that could not be
cleared. **If we take the ACP path, the official adapter is the one.**

Residual uncertainty (MEDIUM): the official README states the Agent SDK is the
vehicle but does not name a credential. That the Agent SDK requires API-key auth
comes from §6's policy, not from the adapter's own docs. Separately, Goose's
docs claim its `claude-acp` provider "uses the same Claude subscription," which
contradicts an Agent-SDK-backed adapter — most likely Goose pins an older
CLI-driving version, but that was not established.

## 8. Open questions

RESOLVED since first draft:

- ~~`claude-agent-acp` — maintainer, licence, current state~~ → §7.5. Apache-2.0,
  no CLA, 2330 stars, pushed same-day, v0.64.0. Actively maintained.
- ~~How much of ACP cabal would need to implement~~ → §7.3. Agent side is ~5
  methods plus `session/update`; client side is a terminal subsystem and a
  permission UI.

Still unverified, in the order they gate work:

3. Whether `cbl chat` streams a primus fan-out usefully or blocks until AGGREGATE
   — determines whether this feels like a team or a slow oracle.
4. Multi-turn state across dispatches for a subprocess/ACP executor.
5. Concurrency: two executors in one repo collide. techne already has
   `working_tree_collisions()` (`core/coord.py:401`) — wire it in rather than
   rediscover it.
6. Cost model, blocked on the §6 MEDIUM item.

## 9. Standing blockers, not caused by this proposal

- `cabal/.env` had a committed `CABAL_MASTER_KEY`. It was rotated and untracked
  on 2026-09-23 (#196), but it remains **in git history**, so deletion alone was
  insufficient. Matters more if cabal's role expands.
- techne CI pairs `on: pull_request` with a self-hosted runner = RCE on flip.

## 10. What is NOT decided

Everything. No shape has been chosen, nothing is filed, nothing is built. The
cheapest next step is either the design conversation continuing on ACP, or a
spike of the dispatch handler (§4.1) to feel the topology working before
committing to it.
