# Proposal: cabal activity log — patterns borrowed from pi

| | |
|---|---|
| **Status** | Draft (patterns-lift, pre-implementation) |
| **Authors** | Richard DiMartino + AI |
| **Date** | 2026-07-04 |
| **Sources** | `earendil-works/pi` (read at source); cabal harness-surface map (both this session) |

## The gap this closes

cabal has **no durable per-activity record**. Ground truth from the harness map:

- `HookName.TOOL_USE` is **defined but never fired** (`hooks/registry.py:44`) — there is no
  structured record of `{tool_name, arguments, result, is_error, latency}` for any tool call.
- Tool-call data exists only transiently inside a checkpoint's `messages` array and is
  **deleted on task success** (`checkpoint_store.drop()`, `llm_persona.py:320`).
- The only generation-level sink is Langfuse, which is **optional and off by default**
  (`app.py:199-208`); with it off, model/messages/tokens/outcome are fully ephemeral.
- `tokens_used` is present in the `LITELLM_POST` payload (`litellm_client.py:200`) but is
  **not forwarded** anywhere durable.
- The `Bookkeeper` `events` table records **dispatch-level** rows only
  (`dispatch_start/success/failure`) — no per-LLM-call, no per-tool data.

Net: after a persona finishes a Task we cannot answer *"what did it call, with what
arguments, what came back, how many tokens, at what cost?"* This blocks debugging, cost
accounting, replay, and clean techne capture.

pi solves exactly this problem, elegantly: **a single append-only JSONL per session that is
simultaneously the activity log, the resume source-of-truth, and a fork tree.** We borrow the
*ideas*, adapted to cabal's bounded task/persona loop — **not** pi's interactive TUI.

## What pi does (source-grounded, HIGH confidence)

- **One append-only JSONL per session.** Line 1 = header `{type:"session", version, id, timestamp, cwd, parentSession?}`; lines 2–N = entries. Written with `appendFileSync`; never rewritten except on fork/migration. (`packages/agent/src/harness/session/jsonl-storage.ts`)
- **Every entry is a tree node:** base shape `{type, id, parentId, timestamp}`. The tree is a pure in-memory overlay built by walking `parentId`; a mutable `leaf` entry marks the current branch tip. No separate tree file.
- **Entry taxonomy:** `message` (role = user | assistant | toolResult), `model_change`, `active_tools_change`, `compaction`, `branch_summary`, `custom` / `custom_message`, `label`, `session_info`, `leaf`.
- **Tool calls live in the assistant `message`** as a `{type:"toolCall", id, name, arguments}` content block; **tool results are a separate `toolResult` message** `{toolCallId, toolName, content}`. Thinking is an inline `{type:"thinking"}` block. **Token usage + cost** ride on the assistant message's `usage` object.
- **Lazy flush:** the file is not created until the first assistant message arrives, so abandoned sessions never litter disk.
- **Replay = `getPathToRoot(leafId)`** — walk `parentId` to root, return the ordered ancestor chain, project to LLM messages.
- **Fork = linearize + back-pointer:** `getEntriesToFork()` calls `getPathToRoot(target.parentId)`, writes only that chain to a new file with `parentSession` pointing back to the source. This *is* the "selective-entry replay."
- **Extension events carry a veto/mutate protocol.** Notably `tool_call` → return `{block:true, reason}` to prevent execution, or mutate `event.input` in place to patch args; `tool_result` → return `{content?, details?, isError?}` to patch the result; `context` / `before_agent_start` → replace messages / system prompt. (`packages/coding-agent/src/core/extensions/runner.ts`)
- **`executionMode: "sequential" | "parallel"`** per tool — if *any* tool in a batch is sequential, the whole batch runs serially. (No "ask/deny/sandbox" modes; permission gating is done via the `tool_call` event, not `executionMode`.)

## Borrow list (ranked by leverage-per-effort)

### B1 — Durable activity log, and actually fire `TOOL_USE` — **CRITICAL, do first**

The core borrow. Give cabal an append-only, durable record of everything a dispatch does.

- **Fire `TOOL_USE`** in the persona tool loop (`llm_persona.py:250-313`) — once per tool call, carrying `{task_id, tool_call_id, tool_name, arguments, result_summary, is_error, latency_ms}`. The hook already exists; it just needs a call site and a consumer.
- **Add an `ActivityLogger`** hook consumer, mirroring `TraceExporter` but writing **durable rows** instead of shipping to Langfuse. It consumes `TOOL_USE` + `LITELLM_POST` and appends structured entries.
- **Storage — reuse cabal's own `events` table** (`models.py:150-167`), which is already `(task_id, event_type, payload_json, created_at)`. Add new event types: `llm_call` (model, tokens, cost, outcome, span_id) and `tool_call` / `tool_result` (the fields above). Unlike checkpoints, **these are never dropped on success** — that's the whole point.

This is cabal-native (SQLite, queryable, already wired), non-invasive (leaves checkpoint/resume untouched), and closes the critical gap on its own.

### B2 — Adopt pi's `{id, parentId}` entry shape now, defer the fork machinery — **cheap option-value**

Have each activity entry carry `{id, parentId}` (parent = the previous entry in the dispatch, or the branch point). We do **not** build fork/branch/tree UI — cabal is a task orchestrator, not a coding TUI (YAGNI). But adopting the *pointer shape* now is nearly free and buys us, later and for free: replay (`getPathToRoot`), branching a task from a mid-point, and format-compatibility with pi (so techne can read either with one adapter). Skipping it now means a migration later.

### B3 — Hook veto/mutate protocol — **medium; enables in-process tool policy**

cabal's hooks are pure side-effects today; no hook can veto or mutate a tool call (harness map §4). Borrow pi's `tool_call` return protocol: let a `TOOL_USE` (pre) handler return `{block, reason}` or a patched-args object; let a post handler patch the result. **Independence angle:** this lets cabal gate/redact/dry-run tools *in-process, without liminal* — defense-in-depth alongside liminal's server-side JWT `allowed_handlers`, and a working tool-policy story when cabal runs standalone. Requires giving `registry.fire()` a return-value contract (currently returns `None`).

### B4 — Token + cost on every `llm_call` record — **cheap, high-value for accounting**

pi stamps `usage {input, output, cacheRead, cacheWrite, cost{…}}` on each assistant message. cabal already has `tokens_used` in `LITELLM_POST` but drops it. When B1 lands, carry tokens (and, if LiteLLM exposes it, cost) onto the `llm_call` entry. Turns "what did this persona cost?" from unanswerable into a `SELECT`.

### B5 — Lazy flush + `executionMode` — **nice-to-have, defer**

- *Lazy flush:* if we ever project the log to a per-task JSONL file (see below), defer file creation until the first real entry — pi's trick to avoid disk litter from abandoned dispatches. Trivial; adopt when/if we build the projection.
- *Parallel tool execution:* cabal runs tool calls serially in a `for` loop today. pi's per-tool `sequential|parallel` flag is a latency optimization worth revisiting only if a dispatch ever issues many independent tool calls. Low priority.

## Optional: JSONL projection (the techne feed — keeps independence intact)

SQLite stays the source of truth (cabal standalone). A thin projector can read a task's
`events` rows and emit a **pi-shaped JSONL file** (header + `{id,parentId,timestamp}`
entries). That file is the clean, portable feed techne's `SourceAdapter` consumes — **without
techne being a dependency of cabal**. Working-together unlocks live memory; neither requires
the other. This is the correct realization of the earlier "cabal generates the activity log,
techne consumes it" idea — the log is cabal-facing first, techne-facing optionally.

## What we deliberately DON'T borrow

- **The TUI tree navigator / session switcher / compaction UI** — cabal is not an interactive coding harness.
- **The jiti TypeScript extension loader** — cabal has `hooks/registry.py`; keep Python.
- **`trust-manager.ts` project-trust** — cabal's trust model is federation/JWT (a different concern entirely).
- **pi's full message-format coupling** — we mirror the *shape* (tool call in assistant content, separate tool result), not pi's exact Anthropic-messages schema.

## Tracked issues

- **B1 → #49** — fire `TOOL_USE` + `ActivityLogger` writing durable `llm_call`/`tool_call`/`tool_result` rows to the `events` table (foundational).
- **B2 → #50** — activity entries carry `{id, parentId}` tree shape (builds on #49).
- **B3 → #52** — hook return-protocol for pre-tool `block`/patch (pairs with #49; related to #43).
- **B4 → #51** — record token usage + cost on each `llm_call` row (builds on #49).
- *(Optional, later — not yet filed)* SQLite→JSONL projector for portability + techne capture.
