"""Guardrails: composable pre/post-dispatch validators.

A guardrail inspects a target value (input messages, output text, or a
router-level payload) and returns a typed Verdict — PASS, BLOCK, or REWRITE.
Unlike HookRegistry side-effects, verdicts can *abort* dispatch (BLOCK) or
*replace* the value before it reaches the LLM (REWRITE).

Guardrails are the verdict-returning complement to HookRegistry callbacks.
They do NOT route through HookRegistry — hooks stay side-effect-only.

Usage::

    ctx = GuardrailContext(persona_name="mason", task_id=task.id, phase="input")
    verdict = await run_guardrails(guardrails, messages, ctx)
    if verdict.type == VerdictType.BLOCK:
        # fail the task with verdict.reason
    elif verdict.rewritten is not None:
        messages = verdict.rewritten  # use the rewritten value

run_guardrails semantics
------------------------
- Guardrails run in registration order.
- First BLOCK short-circuits and is returned immediately; remaining
  guardrails are skipped.
- A REWRITE threads its ``rewritten`` value into the *next* guardrail's
  target, so guardrails compose cleanly.
- If no guardrail blocks, returns ``Verdict(PASS)`` with:
    ``rewritten=<final target>``  if any rewrite occurred during the run
    ``rewritten=None``             if the target was never rewritten

Callers interpret the final Verdict as::

    verdict.type == BLOCK           → fail the task with verdict.reason
    verdict.rewritten is not None   → use verdict.rewritten as the new target
    else                            → proceed with the original target unchanged
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cabal.core.abort import TaskAborted


class VerdictType(StrEnum):
    PASS = "pass"
    BLOCK = "block"
    REWRITE = "rewrite"


@dataclass(frozen=True)
class Verdict:
    """Typed outcome from a guardrail check.

    ``type``      — the decision: pass through, block dispatch, or rewrite.
    ``reason``    — human-readable explanation (required for BLOCK; optional
                   elsewhere).
    ``rewritten`` — replacement value, only meaningful when type=REWRITE or
                   when run_guardrails threads a final rewritten value back
                   as PASS.
    """

    type: VerdictType
    reason: str = ""
    rewritten: Any | None = None

    # Convenience constructors -----------------------------------------------

    @classmethod
    def pass_(cls) -> Verdict:
        """Unconditional pass; no rewrite."""
        return cls(type=VerdictType.PASS)

    @classmethod
    def block(cls, reason: str) -> Verdict:
        """Block dispatch with a human-readable reason."""
        return cls(type=VerdictType.BLOCK, reason=reason)

    @classmethod
    def rewrite(cls, value: Any, reason: str = "") -> Verdict:
        """Replace the target with ``value`` and continue.

        ``value`` must not be ``None``: run_guardrails uses ``rewritten is
        None`` as the "no rewrite" sentinel, so a rewrite-to-None would be
        silently swallowed. None of the real targets (messages list / output
        str / Task) is meaningfully None, so we reject it loudly instead.
        """
        if value is None:
            raise ValueError(
                "Verdict.rewrite(None) is unsupported — None is the no-rewrite sentinel"
            )
        return cls(type=VerdictType.REWRITE, rewritten=value, reason=reason)


@dataclass
class GuardrailContext:
    """Contextual metadata passed to every guardrail check.

    ``persona_name``   — name of the persona running the guardrail, or the
                         caller identifier at the router level.
    ``task_id``        — A2A task id; None if unavailable (e.g., early router
                         rejection before a Task is resolved).
    ``phase``          — where in the lifecycle the check runs:
                         "input"       — before the LLM sees input messages
                         "output"      — after the LLM emits its final text
                         "router"      — at the dispatch router before the A2A hop
                         "tool_call"   — before mcp_proxy.call_tool (#52)
                         "tool_result" — after call_tool, before the result is
                                         appended to the conversation (#52)
    ``tool_name``      — the name of the tool being called/returning a result;
                         populated for "tool_call" and "tool_result" phases (#43).
                         None in all other phases.
    ``tool_arguments`` — the arguments dict for the current tool call; populated
                         for "tool_call" and "tool_result" phases (#43).
                         None in all other phases.
    """

    persona_name: str
    task_id: str | None
    phase: str  # "input" | "output" | "router" | "tool_call" | "tool_result"
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None


@runtime_checkable
class Guardrail(Protocol):
    """Protocol for verdict-returning validators.

    Implement ``name`` as a plain str attribute and ``check`` as an async
    method.  ``target`` is the value being validated:
      - "input" phase  → list[dict[str, Any]]  (chat messages)
      - "output" phase → str                   (final LLM text)
      - "router" phase → Task                  (the A2A Task envelope)
    """

    name: str

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict: ...


async def run_guardrails(
    guardrails: list[Guardrail],
    target: Any,
    ctx: GuardrailContext,
) -> Verdict:
    """Run ``guardrails`` in order; return the final Verdict.

    Semantics (documented at the module level):
    - First BLOCK short-circuits — remaining guardrails are skipped.
    - A REWRITE threads its value into the next guardrail's target.
    - A guardrail whose ``check()`` RAISES is treated as a **fail-closed
      BLOCK** naming the guardrail — an unexpected error in a safety check
      must never crash the dispatch it protects (which would leak the
      session). It short-circuits like any other block.
    - All pass → ``Verdict(PASS, rewritten=<final>)`` if any rewrites
      occurred; ``Verdict(PASS)`` (rewritten=None) if the target was never
      rewritten.

    Callers interpret ``rewritten`` as follows:
        not None  → the threaded target after all rewrites; use this value.
        None      → no rewrites; use the original target unchanged.
    """
    current = target
    any_rewrite = False

    for guardrail in guardrails:
        try:
            v = await guardrail.check(current, ctx)
        except TaskAborted:
            # Cooperative cancellation is a first-class propagating signal —
            # the router turns it into a resumable *canceled* Task. Never mask
            # it into a policy BLOCK. (asyncio.CancelledError is BaseException,
            # so the broad except below already lets it through.)
            raise
        except Exception as exc:  # noqa: BLE001
            # Fail closed: convert any unexpected raise into a BLOCK naming the
            # guardrail, rather than letting it propagate out of the persona /
            # router call site and crash the whole Task. Retires the crash class
            # that #57 (malformed rewrite) and #43 (raising validator) each had
            # to defend against per-guardrail (cabal#62).
            name = getattr(guardrail, "name", type(guardrail).__name__)
            return Verdict.block(
                f"guardrail {name!r} raised {type(exc).__name__}: {exc}"
            )
        if v.type == VerdictType.BLOCK:
            return v
        if v.type == VerdictType.REWRITE:
            current = v.rewritten
            any_rewrite = True
        # VerdictType.PASS — continue with current target unchanged.

    if any_rewrite:
        return Verdict(type=VerdictType.PASS, rewritten=current)
    return Verdict(type=VerdictType.PASS)
