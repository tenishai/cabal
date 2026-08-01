"""Mason primus — SRE / reliability engineering persona.

Thin subclass of PrimusPersona. All fan-out machinery (PLAN → DISPATCH →
AGGREGATE, doom-loop guard, hook firing) lives in cabal.agents.primus.

Lifecycle per dispatch: see cabal.agents.primus docstring.
"""

from __future__ import annotations

from cabal.agents.primus import (
    _DOOM_LOOP_DEFAULT,
    PrimusPersona,
    _DoomLoopError,
)
from cabal.agents.subordinate import Subordinate
from cabal.core.guardrails_builtin import DoomLoopGuardrail
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient

# Re-export so any code that imports these from mason keeps working.
__all__ = [
    "MasonPrimusPersona",
    "_DoomLoopError",
    "_DOOM_LOOP_DEFAULT",
    "DEFAULT_MODEL",
    "MASON_SYSTEM_PROMPT",
    "MASON_AGGREGATE_PROMPT",
]

DEFAULT_MODEL = "anthropic/claude-opus-4-7"

MASON_SYSTEM_PROMPT = """\
You are Cabal's Mason: SRE and reliability-engineering primus — infrastructure,
platform, deployment and operational correctness.

You don't write code yourself. You DELEGATE to subordinates:
  - implementer: produces concrete code/diffs/steps.
  - reviewer: critiques the implementer's output for real problems.

For each operator request, your job is to PLAN: in 1-3 sentences, restate
the task as a precise instruction the implementer should follow. Be
specific about acceptance criteria.
"""

MASON_AGGREGATE_PROMPT = """\
You are Cabal's Mason: SRE and reliability-engineering primus — infrastructure,
platform, deployment and operational correctness.

The implementer has returned output and (if a reviewer ran) you have
the reviewer's critique. Summarize the result for the operator in
2-4 sentences. Quote the implementer's specific code/steps when useful.
If the reviewer raised concrete issues, flag them; if they're nitpicks,
ignore them. If the reviewer found nothing serious, say so plainly —
don't manufacture problems.
"""


class MasonPrimusPersona(PrimusPersona):
    """Mason — SRE / reliability primus on the shared PrimusPersona base.

    `subordinates` maps role name → Subordinate instance. Required to have
    at least 'implementer'.

    doom_loop controls autonomous-loop protection at the subordinate boundary
    (cabal#59). By default a fresh ``DoomLoopGuardrail()`` is created so
    repeated identical delegations from a looping caller are blocked.
    Pass ``doom_loop=None`` to disable the guard entirely (e.g. for tests
    that intentionally dispatch the same instruction many times).
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
    ) -> None:
        super().__init__(
            llm_client,
            subordinates,
            name="mason-primus",
            system_prompt=MASON_SYSTEM_PROMPT,
            aggregate_prompt=MASON_AGGREGATE_PROMPT,
            hook_registry=hook_registry,
            model=model,
            default_model=DEFAULT_MODEL,
            max_tokens=max_tokens,
            doom_loop=doom_loop,
        )
