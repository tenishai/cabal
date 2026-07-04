"""Guardrails: composable pre/post-dispatch validators.

A guardrail inspects a target value (input messages, output text, or a
router-level payload) and returns a typed Verdict — PASS, BLOCK, or REWRITE.
Unlike HookRegistry side-effects, verdicts can *abort* dispatch (BLOCK) or
*replace* the value before it reaches the LLM (REWRITE).

Guardrails are the verdict-returning complement to HookRegistry callbacks.
They do NOT route through HookRegistry — hooks stay side-effect-only.

Usage::

    ctx = GuardrailContext(persona_name="rook", task_id=task.id, phase="input")
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
        """Replace the target with ``value`` and continue."""
        return cls(type=VerdictType.REWRITE, rewritten=value, reason=reason)


@dataclass
class GuardrailContext:
    """Contextual metadata passed to every guardrail check.

    ``persona_name`` — name of the persona running the guardrail, or the
                       caller identifier at the router level.
    ``task_id``      — A2A task id; None if unavailable (e.g., early router
                       rejection before a Task is resolved).
    ``phase``        — where in the lifecycle the check runs:
                       "input"  — before the LLM sees input messages
                       "output" — after the LLM emits its final text
                       "router" — at the dispatch router before the A2A hop
    """

    persona_name: str
    task_id: str | None
    phase: str  # "input" | "output" | "router"


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
        v = await guardrail.check(current, ctx)
        if v.type == VerdictType.BLOCK:
            return v
        if v.type == VerdictType.REWRITE:
            current = v.rewritten
            any_rewrite = True
        # VerdictType.PASS — continue with current target unchanged.

    if any_rewrite:
        return Verdict(type=VerdictType.PASS, rewritten=current)
    return Verdict(type=VerdictType.PASS)
