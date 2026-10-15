# Proposal: typed guardrails — verdict-returning validators

| | |
|---|---|
| **Status** | Draft (foundation, pre-implementation) |
| **Authors** | Richard DiMartino + AI |
| **Date** | 2026-07-04 |
| **Issues** | #39 (foundation), #52, #43, #42 |
| **Sources** | OpenAI Agents SDK `Guardrail` (#39); SWE-agent validate-before-edit (#43); opencode doom-loop (#42) |

## The decision this settles

cabal has exactly one interception mechanism today — `HookRegistry` — and it is
**side-effect only**: `fire()` returns `None`, and the sole "veto" is a sync
hook raising `HookError`, which aborts the *whole Task*, not one call
(`hooks/registry.py`). Two open issues ask for verdict-returning validators:

- **#52** — block/patch a single *tool call*.
- **#39** — `pass|block|rewrite` before an LLM sees input / after it emits a result, and at the router before the A2A hop.

#52's original framing was "extend `HookRegistry.fire()` to return verdicts."
We are **not** doing that. #49's `ActivityLogger` is now a pure side-effect
consumer of `TOOL_USE`/`LITELLM_POST`; folding verdicts into the same `fire()`
tangles two concerns (observe vs. decide) and would make every hook call carry
veto semantics. Instead we build a **separate, dedicated `Guardrail`
abstraction** (as #39 explicitly asks — *"not replacing HookRegistry; the
verdict-returning complement"*). Hooks stay side-effects; guardrails are
verdicts.

**Re-scope:** #39 is the **foundation** (the `Guardrail`/`Verdict` types + the
two application points). #52 becomes "apply guardrails at the tool-call
boundary." #43 and #42 are **concrete guardrails** built on the foundation.

## The abstraction (#39 — foundation)

```python
# src/cabal/core/guardrails.py
class VerdictType(StrEnum):
    PASS = "pass"      # allow unchanged
    BLOCK = "block"    # stop; carry `reason`
    REWRITE = "rewrite"  # continue with `rewritten`

@dataclass(frozen=True)
class Verdict:
    type: VerdictType
    reason: str = ""
    rewritten: Any | None = None   # only for REWRITE

class Guardrail(Protocol):
    name: str
    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict: ...

async def run_guardrails(guardrails, target, ctx) -> Verdict:
    """Run in order; first BLOCK short-circuits; REWRITE threads the rewritten
    value into the next guardrail's target; else PASS."""
```

Guardrails are **independently unit-testable**, **stackable per persona**, and
enforced **before the network hop** — the properties #39 calls out.

## Enforcement points

| Point | Where | Covers |
|---|---|---|
| **Pre-dispatch (input)** | `LLMPersona`, before each LLM call | #39 |
| **Post-result (output)** | `LLMPersona`, after each result | #39 |
| **Router (pre-A2A-hop)** | `core/broker/router.py`, before dispatching across the hop | #39, #42 |
| **Pre-tool-call** | `LLMPersona` tool loop, before `mcp_proxy.call_tool` | #52 |
| **Post-tool-result** | `LLMPersona` tool loop, before the observation is recorded | #52, #43 |

BLOCK at the pre-tool point → skip `call_tool`, inject an error `tool` message
(pi's model, and #52's acceptance criteria). BLOCK at pre-dispatch → fail the
Task with the reason. REWRITE → continue with the rewritten input/args/result.

## Build order

1. **#39 — foundation** (this ships first, solo): `guardrails.py` (`Verdict`,
   `Guardrail`, `run_guardrails`) + `LLMPersona.guardrails` applied at
   pre-dispatch and post-result + a router-level input guardrail. Composable +
   unit-tested in isolation. Nothing else moves until this lands.
2. **#52** — apply guardrails at the tool-call boundary (pre-tool block/patch,
   post-tool-result rewrite). Also emits the `tool_call` row *before*
   `call_tool` — completing the crash-gap detection #49 deferred here.
3. **#43** — a concrete `ValidateBeforeRecord` guardrail: for mutating tools,
   run configured lint/type/test and BLOCK-with-structured-error before the
   observation is recorded. Opt-in per persona + per tool.
4. **#42** — a concrete doom-loop guardrail at the router point: per-session
   `(target, payload-hash)` counter; at N identical, BLOCK→escalate (not
   retry), surfacing the reason back into the caller's context.

#52/#43/#42 can parallelize once #39 is merged (they touch different
enforcement points), same worktree-isolation + verify + Prudence loop.

## What we deliberately keep separate

- **Hooks stay side-effect-only.** `ActivityLogger` (#49) is untouched;
  guardrails never route through `HookRegistry.fire()`.
- **liminal authz still stands.** In-process guardrails are defense-in-depth +
  a standalone tool-policy story (independence principle), not a replacement
  for liminal's server-side JWT `allowed_handlers`.
