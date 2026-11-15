"""Wright primus — general-purpose software engineering persona.

Thin subclass of PrimusPersona. All fan-out machinery (PLAN → DISPATCH →
AGGREGATE, doom-loop guard, hook firing) lives in cabal.agents.primus.

Wright acts as a senior software engineer who breaks concrete coding tasks
into precise instructions for an implementer subordinate, then aggregates
the result for the operator. It does NOT write code itself as the primus.
An optional reviewer subordinate critiques the implementer's output for
correctness, edge cases, and missed invariants.

Lifecycle per dispatch: see cabal.agents.primus docstring.
"""

from __future__ import annotations

from cabal.agents.primus import (
    _DOOM_LOOP_DEFAULT,
    MAX_LEGS_PER_FANOUT,
    PrimusPersona,
)
from cabal.agents.subordinate import Subordinate
from cabal.core.guardrails_builtin import (
    DelegationBudgetGuardrail,
    DelegationDepthGuardrail,
    DoomLoopGuardrail,
)
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient

__all__ = [
    "WrightPrimusPersona",
    "DEFAULT_MODEL",
    "WRIGHT_SYSTEM_PROMPT",
    "WRIGHT_AGGREGATE_PROMPT",
]

DEFAULT_MODEL = "anthropic/claude-sonnet-5"

WRIGHT_SYSTEM_PROMPT = """\
You are Cabal's Wright: general-engineering primus and senior software engineer.

You don't write code yourself. You DELEGATE to subordinates:
  - implementer: produces concrete code, diffs, or working steps.
  - reviewer: critiques the implementer's output for correctness and edge cases.

For each operator request, your job is to PLAN: in 1-3 sentences, restate
the task as a precise instruction the implementer should follow. Be specific
about:
  - which files or functions to touch
  - acceptance criteria (what "done" looks like)
  - any constraints or invariants the implementer must preserve

Your planning instruction must be self-contained enough that the implementer
can carry it out without asking clarifying questions.
"""

WRIGHT_AGGREGATE_PROMPT = """\
You are Cabal's Wright: general-engineering primus and senior software engineer.

The implementer has returned output and (if a reviewer ran) you have the
reviewer's critique. Summarize the result for the operator in 2-4 sentences.
Quote the implementer's specific code or steps when useful.
If the reviewer raised concrete issues, flag them clearly; if they are nitpicks
or style-only, omit them. If the reviewer found nothing serious, say so plainly
— don't manufacture problems.
"""


class WrightPrimusPersona(PrimusPersona):
    """Wright — general-engineering primus on the shared PrimusPersona base.

    `subordinates` maps role name → Subordinate instance. Required to have
    at least 'implementer'.

    doom_loop controls autonomous-loop protection at the subordinate boundary
    (cabal#59). By default a fresh ``DoomLoopGuardrail()`` is created so
    repeated identical delegations from a looping caller are blocked.
    Pass ``doom_loop=None`` to disable the guard entirely.
    """

    def __init__(
        self,
        llm_client: LiteLLMClient,
        subordinates: dict[str, Subordinate],
        *,
        hook_registry: HookRegistry | None = None,
        model: str | None = None,
        max_tokens: int = 8192,
        doom_loop: DoomLoopGuardrail | None = _DOOM_LOOP_DEFAULT,  # type: ignore[assignment]
        # cabal#156 — forwarded so a caller can lower the breadth ceiling, and
        # since cabal#172 that caller is usually `persona_factory` passing this
        # persona's `max_legs_override`. It caps how many legs the persona may
        # DECLARE, checked once in the constructor; a persona declaring more is
        # skipped at boot, not trimmed (#178 r1).
        max_legs: int = MAX_LEGS_PER_FANOUT,
        # cabal#158 — forwarded so the factory can share the ROUTER's budget
        # instance; a second instance would double a conversation's allowance.
        delegation_budget: DelegationBudgetGuardrail | None = None,
        delegation_depth: DelegationDepthGuardrail | None = None,
        # Issue 394: a token budget per leg and for the whole fan-out.
        leg_token_budget: int | None = None,
        fanout_token_budget: int | None = None,
    ) -> None:
        super().__init__(
            llm_client,
            subordinates,
            name="wright-primus",
            system_prompt=WRIGHT_SYSTEM_PROMPT,
            aggregate_prompt=WRIGHT_AGGREGATE_PROMPT,
            hook_registry=hook_registry,
            model=model,
            default_model=DEFAULT_MODEL,
            max_tokens=max_tokens,
            doom_loop=doom_loop,
            max_legs=max_legs,
            delegation_budget=delegation_budget,
            delegation_depth=delegation_depth,
            leg_token_budget=leg_token_budget,
            fanout_token_budget=fanout_token_budget,
        )
