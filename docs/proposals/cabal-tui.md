# Proposal: Cabal TUI — multi-pane Textual UI over existing event bus + SSE infrastructure

**Status**: Draft — design captured, no code in flight yet.
**Author**: Claude (Opus 4.7) + Richard, paired session 2026-06-14.
**Tracking issues**: TBD.
**Related proposals**: [`investigation-engine.md`](./investigation-engine.md) (multi-instance personas + A2A streaming + mailbox — the back-end this TUI consumes).
**Sibling memory**: [[feedback-cabal-standalone-terminal]] (cabal is its own thing, with its own primary UX); [[feedback-survey-before-propose]] (this proposal shrank after surveying actual cabal code).
**Revisions**:
- 2026-06-14 (final) — **Reframed after surveying cabal code.** The previously-proposed "Operator Channel" was substantially reinventing infrastructure that already ships: `EventBus`, `/admin/events/stream`, `/admin/conversations`, `cbl tail`, the persona SSE endpoints. The TUI's back-end is mostly already built; the net-new work is Textual UI + small extensions to existing primitives.

> **Scope**: A multi-pane terminal UI for cabal, written in Textual (Python). Plugs into cabal's *existing* event-streaming infrastructure (the in-process `EventBus`, the per-persona SSE endpoints, `/admin/events/stream`, `/admin/conversations`) plus a handful of small extensions covered in [`investigation-engine.md`](./investigation-engine.md) (named channels on the bus, mailbox subscription). **The broker is the API. The TUI is the first multi-pane client of an API that mostly exists.**

---

## 1. Why now

Richard's current workflow opens 4 Claude Code tabs, each running a different Rook persona. Context-switching between them is painful. He wants:

1. One terminal window showing all stacks at once
2. A persistent **Primus** voice per stack he can talk to
3. The Primus can spawn ephemeral sub-agents that run in background and report back via mailbox — operator stays focused on the conversation they're in
4. Operator drops into any stack's pane and chats; drops out; everything keeps running
5. A meta-pane shows inter-Primus chatter; operator can interject in real time

The architecture exists for the agents — cabal's persona engine, A2A streaming, sub-agent spawning, the Archivist as a mailbox. What's missing is a **unified operator-facing surface** that gives the operator one view of the whole colony.

---

## 2. What's already built; what the TUI extends

The TUI is a Textual client that pulls events from cabal's *existing* event-streaming layer. Most of the back-end is already shipping; the TUI plugs in.

| Concern | Status | Where |
|---|---|---|
| In-process pub/sub event bus | **Built** | `cabal/core/observability/event_bus.py` — `subscribe()`, `publish()`, `stream()` async iterator |
| Fleet-wide event SSE endpoint | **Built** | `/admin/events/stream` (Phase U commit 29) |
| Per-persona event SSE endpoint | **Built** | `POST /agents/{name}/stream` (Phase S commit 28) — streams the persona's `astream_task` output |
| Conversations list | **Built** | `/admin/conversations` (Phase U) |
| `cbl tail` (CLI consumer of `/admin/events/stream`) | **Built** | `cabal/cli/main.py` |
| `cbl chat` multi-turn streaming | **Built** | (Phase T) — consumes SSE, prints deltas live, history accumulation |
| `cbl conversations` browse | **Built** | (Phase U) |
| Operator auth + JWT minting | **Built** | `cabal/core/auth/` + `cbl login` |
| Persona AgentCard registry (bulletin board) | **Built** | `cabal/core/bulletin/store.py` |
| Hot reload of personas | **Built** | `POST /admin/reload/identity` + `cbl reload identity` |

The TUI doesn't introduce a "new aggregation protocol." It subscribes to the existing event bus + the existing SSE endpoints, just with a Textual front-end that lays them out as panes.

**What the TUI does need that doesn't quite exist yet** (small extensions covered in [investigation-engine.md](./investigation-engine.md)):

- **Named channels on `EventBus`** (PR-F in investigation-engine.md) — today the bus is single-channel; the TUI wants `meta` channel filtering separate from the default fleet stream
- **Mailbox subscription** (PR-E) — the TUI's mailbox pane needs the Archivist mailbox-kind support
- **Multi-instance personas** (PR-D) — `rook-primus` + `rook-secundus` + `rook-tertius` as concurrent subscriptions in separate panes
- **A2A-spec streaming** (PR-A + PR-B) — useful when the TUI is run remotely (`cabal connect`); the existing non-spec SSE is fine for in-process

All five are covered in the sibling proposal; this proposal assumes they land.

---

## 3. Run modes

**Default**: `cabal` (or `cbl` — they're aliases) opens the TUI against the local daemon. If the daemon isn't running, `cabal` starts it as a subprocess. If it's already running, `cabal` connects.

| Command | What it does |
|---|---|
| `cabal` / `cbl` | Open TUI against local daemon (starts daemon if not running) |
| `cabal serve` / `cbl daemon up` | Run the daemon explicitly (no TUI) — already exists |
| `cabal connect <ws-url>` | TUI against a remote daemon (e.g. SSH'd-to server) |
| `cabal dispatch <persona> "..."` / `cbl dispatch` | Non-TUI one-shot dispatch — already exists |
| `cabal personas add rook secundus` | Spawn a new persona instance — see investigation-engine.md PR-D |
| `cabal personas list` / `cbl status` | List personas — already exists |
| `cabal tail` / `cbl tail` | Non-TUI event stream (existing) |

The TUI binds to a `BrokerClient` interface (a thin Python class with `subscribe`, `send_message`, etc.). Two implementations:

- **`InProcessBrokerClient`** — direct Python calls within the same process. Used when `cabal` launches the daemon itself. Zero serialization overhead.
- **`RemoteBrokerClient`** — HTTP + SSE against the existing `/admin/events/stream`, `POST /agents/{name}/stream`, `POST /agents/{name}` endpoints. Used by `cabal connect`. Reuses the existing auth + JWT path.

No new wire protocol. Both implementations are thin wrappers over what `cbl tail` and `cbl chat` already do — just shaped for Textual consumption.

---

## 4. Pane model in the TUI

The TUI is one client of the Operator Channel. It maintains a **layout** of panes, each subscribed to one source.

### Pane types

| Pane type | Subscribes to | Content |
|---|---|---|
| **Primus pane** | One `persona` (e.g. `rook-primus`) | Streamed `TaskStatusUpdateEvent`s rendered as conversational turns + tool calls + thoughts; input line at the bottom forwards to `send_message` |
| **Meta pane** | Meta-channel | Cross-Primus chatter; operator interjects via `send_meta` |
| **Mailbox pane** | One mailbox (operator's or a Primus's) | List of items with unread badges, sender persona, summary; opening an item shows full content + lets operator route ("acknowledge to Rook-Primus") |
| **Roster pane** | `personas_changed` events | Live list of all known personas with status (active / idle / retired); double-click opens a Primus pane for that persona |
| **Status bar** | Aggregate | Connection state, token usage today, active sub-agent count, mailbox unread count |

### Default layout

```
┌─────────────────────────────────────────────────────────┐
│ Status bar: connected ● 13.4k tokens today ● 2 unread   │
├──────────────┬──────────────┬───────────────────────────┤
│ Rook-Primus  │ Raven-Primus │ Meta-channel              │
│              │              │                            │
│ ...chat...   │ ...chat...   │ Rook: "Raven, I'm…"        │
│              │              │ Raven: "Got it."           │
│              │              │ Operator: "wait, focus…"   │
│              │              │                            │
│ > _          │ > _          │ > _                        │
├──────────────┴──────────────┼───────────────────────────┤
│ Mailbox (operator)          │ Roster                     │
│ ● Sherlock-Quintus (3min)   │ Rook-Primus    [active]    │
│ ● Documenter-7 (12min)      │ Rook-Secundus  [idle]      │
│   Investigator-3 (1h)       │ Raven-Primus   [active]    │
│                              │ Sub-agents     [4 running] │
└──────────────────────────────┴────────────────────────────┘
```

The layout is configurable per-session and persisted to `~/.cbl/tui/layout.toml`. Default mode is two Primus panes + meta + mailbox + roster; operator can rearrange via `Ctrl-L` (layout mode).

### Focus and input

- `Ctrl-Tab` / `Ctrl-Shift-Tab` rotate focus between panes
- `Ctrl-1`...`Ctrl-9` jump to numbered pane
- `Esc` exits text-input mode to layout/navigation mode
- Type in a focused Primus pane → operator's words go to that Primus via `send_message`
- Type in the meta pane → words go to all subscribed Primuses via `send_meta` (every Primus's next turn sees the operator's interjection)
- Type in the mailbox pane → contextual actions on the selected item (forward to Primus X; archive; act on it as a new investigation)

### Sub-agent visualization

When a Primus spawns a sub-agent (Sherlock investigation, documenter pass, verifier run), the Primus pane shows a *collapsed* status line:

```
Rook-Primus
  > investigating staging slowdown... ● Sherlock-Quintus [running, 32s]
```

Clicking expands an inline mini-pane that streams the sub-agent's events live. When the sub-agent retires (drops its finding into a mailbox), the inline pane collapses and a mailbox notification fires.

---

## 5. Implementation in Textual

Textual is Python-native, MIT-licensed, async-first, and ships with the primitives we need.

### Project layout

```
cabal/tui/
  __init__.py
  app.py              # The Textual App (entry: `cabal`)
  client/
    base.py           # BrokerClient Protocol
    inprocess.py      # InProcessBrokerClient — direct calls on EventBus + LLMPersona.astream_task
    remote.py         # RemoteBrokerClient — httpx/SSE against /admin/events/stream + persona SSE
  panes/
    primus.py         # PrimusPane widget — subscribes to one persona's astream_task
    meta.py           # MetaPane widget — subscribes to EventBus 'meta' channel
    mailbox.py        # MailboxPane widget — queries Archivist mailbox MCP tools
    roster.py         # RosterPane widget — reads /admin/agents + reacts to bulletin updates
  layout.py           # Layout persistence + Ctrl-L editor (~/.cbl/tui/layouts/)
  rendering/
    cabal_event.py    # Render cabal_event metadata (tool_start, tool_end, thought)
    markdown.py       # Streaming-Markdown wrapper (Textual v4)
  styles.css          # Textual CSS
```

### Key Textual primitives we leverage

- **`Worker`** — each Primus subscription is a background async worker pumping events from `BrokerClient.subscribe(persona)` into the pane widget. Cancellable cleanly on unsubscribe.
- **CSS-grid layout** — the pane grid is declarative CSS; resizing and rearranging is a layout mode, not a code change.
- **`MarkdownStream` (v4)** — streams Markdown into a widget as tokens arrive. LLM output renders incrementally; no waiting for full turns.
- **Async message system** — Textual's message passing carries `BrokerClient` events to panes cleanly.

### Auth and connection

- **In-process mode** (default `cabal`): the TUI runs in the daemon's process; no auth handshake — direct Python access to `EventBus` and persona handlers.
- **Remote mode** (`cabal connect <url>`): standard `cbl login --code <CODE>` flow (already exists). JWT stored at `~/.cbl/tui/credentials.json`; refreshed on expiry. The `RemoteBrokerClient` carries it on every HTTP/SSE call.

### Performance

- The hot path is Markdown rendering of streaming LLM tokens — Textual v4's incremental rendering handles this well.
- Backpressure: if the TUI's render loop can't keep up, drop intermediate events but never drop terminal events. The existing `EventBus` already drops events for slow subscribers (per its docstring) — same shape applies here.
- Layout persistence is debounced to avoid disk thrash.

---

## 6. Implementation sequence

Three PRs. Dependencies on investigation-engine.md noted.

### PR-T1 — Textual TUI MVP (`cabal/tui/` skeleton + InProcessBrokerClient)

- `cabal/tui/` package skeleton (`app.py`, `panes/{primus,mailbox}.py`, `client/inprocess.py`, basic CSS)
- `cabal` CLI entry — opens the TUI against the local daemon; spawns daemon if not running
- Two-pane default layout: one Primus pane (default subscription: `rook-primus`), one Mailbox pane (default: operator's mailbox)
- Ctrl-Tab focus rotation; type → `send_message` to the focused pane's persona
- v4 Markdown streaming for LLM output
- Reuses existing `EventBus` (in-process) + `astream_task` (per-persona) + JWT auth
- **Dependency**: investigation-engine.md PR-E (mailbox kind on Archivist) — otherwise the Mailbox pane has nothing to render
- Tests: snapshot tests on layout rendering; unit tests on `InProcessBrokerClient`; one end-to-end test with a scripted fake persona

### PR-T2 — Multi-instance Primus panes + Meta pane

- Roster pane (live persona list — reads `/admin/agents` + reacts to bulletin-board updates)
- Multiple Primus panes simultaneously: `rook-primus`, `rook-secundus`, `raven-primus` — each its own `astream_task` subscription
- Meta pane subscribed to the `meta` channel of `EventBus`
- Type in Meta pane → `send_meta` (broadcasts to all subscribed Primuses' next turn)
- **Dependency**: investigation-engine.md PR-D (multi-instance personas) + PR-F (named channels on EventBus)
- Tests: 3 concurrent Primus subscriptions don't crosstalk; meta-broadcast reaches all subscribers

### PR-T3 — Polish

- Sub-agent inline expansion in Primus panes (expand/collapse subordinate event streams)
- Layout mode (`Ctrl-L`) — rearrange / resize / save layouts
- Status bar (tokens-today via BudgetGuard, unread mailbox count, active sub-agent count)
- Persistent layouts under `~/.cbl/tui/layouts/`
- `RemoteBrokerClient` implementation + `cabal connect <ws-url>` flow
- Tests: layout persistence round-trip; sub-agent collapse/expand; keyboard shortcuts; remote connect against a running daemon

---

## 7. Where this fits in the broader UI story

Once the TUI is shipping, the same back-end (existing SSE + EventBus + per-persona endpoints) trivially supports other clients. Each ~1-2 weeks of Claude-time:

| Future UI | Notes |
|---|---|
| **VSCode / VSCodium plugin** | Sidebar showing one or two Primus panes; operator types in the sidebar to `POST /agents/{name}` (existing). Activity bar badge for mailbox unread count via `/admin/events/stream`. Useful for "I'm coding; let me ask Rook-Primus a quick thing without leaving the IDE." |
| **openwebui-style web UI** | Browser app consuming the same SSE endpoints. Operator picks a persona per chat, can split-view multiple chats. |
| **iPhone Claude.app via MCP** | Limenal bridges MCP → cabal personas (already wired via `MCPToolProxy`). iPhone gets one Primus at a time. Push notifications for mailbox additions via APNs. |
| **Home Assistant dashboard widget** | Subscribe-only; reads `/admin/events/stream` + the mailbox MCP tools (PR-E). Ambient awareness of the colony. |
| **Slack/Telegram bot** | Bot service subscribes to a persona's SSE stream; relays operator's DMs to the persona via `POST /agents/{name}`. The "remote persona" pattern. |

These do NOT need to be built now. They're enabled by the back-end that already mostly exists. The TUI is the first multi-pane consumer.

---

## 8. Open design questions

### 1. Operator's voice in the meta-pane

When the operator types in the Meta pane, every subscribed Primus sees the operator's interjection on their next turn. Should the operator be:
- A **peer participant** — Primuses treat the operator's messages like any other Primus's contribution (collaborative reasoning)
- A **director** — Primuses treat the operator's messages as authoritative instructions that override their plans

**Recommendation: peer participant by default**, with a `>director` prefix that switches the operator's message to authoritative for that turn. Matches the partnership stance.

### 2. Sub-agent identity in the UI

Per investigation-engine.md, sub-agents (Subordinate instances) have no persistent identity — they're owned by their primus, no bulletin-board entry. But the UI needs *some* label so the operator can track "investigator-1 is checking the slowdown" vs. "verifier-3 is reviewing a config diff."

**Recommendation**: ephemeral identity tuple `(parent_primus, role, ordinal)` rendered as `investigator-1 [under Rook-Primus]`. The ordinal increments per spawn within a primus session.

### 3. First-run experience

Today `cbl daemon up` + `cbl login --code <CODE>` is the bootstrap sequence. For the TUI, do we want to:
- Keep the explicit two-step (operator runs `cabal serve` then `cabal`)
- Auto-bootstrap from the TUI (`cabal` launches, spawns daemon, mints token, opens panes)

**Recommendation: auto-bootstrap when `cabal` is run with no daemon detected.** Check for a pidfile at `~/.cbl/cabald.pid`; if absent or stale, spawn the daemon, intercept the bootstrap code from its logs, mint a token, store it at `~/.cbl/tui/credentials.json`, then open the UI. Explicit `cabal serve` stays available for the server-mode case.

### Settled (no longer open)

- **Naming**: binary is `cabal` (with `cbl` as an alias). Resource directory is `~/.cbl/`. Rationale: `~/.cabal/` clashes with Haskell's build tool — see [[feedback-cabal-standalone-terminal]].
- **Framework**: Textual (Python) for V1. Migrate to Bubble Tea (Go) later if product-shape demands compiled-binary distribution.
- **Aggregation protocol**: no new protocol. Reuses the existing `EventBus` + per-persona SSE + `/admin/events/stream`.
- **Auth**: per-operator JWTs (already shipping via `cbl login`).

---

## 9. Things deliberately NOT in scope

- **Forking Crush, sst/opencode, gptme, or any other existing client.** None match the multi-Primus + mailbox + meta-pane shape; adapting them would be more work than starting fresh in Textual. See the 2026-06-14 terminal-AI-client survey for the full evaluation.
- **cmux as the window manager.** Could sit *under* the TUI (GPU-accelerated pane rendering on macOS), but not load-bearing. Operator can use any terminal they want.
- **A web UI in V1.** openwebui-style web UI is a future addition once the TUI shape is validated.
- **Multi-operator scenarios.** Single-user platform. Per-operator JWT auth (already shipping via `cbl login`) is enough.
- **A Bubble Tea / Go TUI in V1.** Considered, deferred. Textual is the V1 choice — same language as the backend, in-process speed, fast prototyping. If the product feels too "Python script" later, migrate to Bubble Tea + Go (with the daemon staying Python and the TUI connecting via the existing SSE endpoints). The `BrokerClient` Protocol boundary makes that migration tractable.
- **A from-scratch "Operator Channel" protocol.** The existing event bus, per-persona SSE, and admin streaming endpoints cover what the TUI needs. Small extensions (named channels, mailbox subscription) live in investigation-engine.md.

---

## 10. References

### Surveys
- `architectural-patterns.md` — the engine-side patterns this TUI consumes (typed SSE events, mailbox-via-Archivist, etc.)
- TUI client survey done 2026-06-14: opencode (both flavors), Crush, gptme, aider, llm (Willison), plandex, Goose, cmux (manaflow-ai). None matched the multi-pane multi-Primus shape.

### Frameworks
- [Textual](https://textual.textualize.io/) — Python TUI framework (MIT)
- [Textual v4 Streaming Markdown](https://simonwillison.net/2025/Jul/22/textual-v4/)

### Memory
- [[feedback-cabal-standalone-terminal]] — cabal as its own thing; the TUI is the primary UX, but the broker channel is the API any UI can use
- [[project-rookery-metaphor]] — the canopy view of the colony is exactly what this TUI delivers
- [[feedback-messaging-modular]] — transport-agnostic patterns first (Operator Channel is one; Slack/Telegram bots are later A2A consumers)
- [[feedback-partnership-stance]] — operator-as-peer-participant in the meta-channel
