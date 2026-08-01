"""Subordinate persona — a plain LLMPersona instantiated per primus role.

Subordinates are not registered with the bulletin board (no AgentCard
of their own); the primus owns them. They exist only inside the primus
process and the primus calls their `handle_task` directly. The dispatch
router never sees them.

This is the same architectural decision that lets `cabal/agents/echo.py`
sit outside the persona registry — these are not first-class A2A peers,
they're internal implementation of a peer.
"""

from __future__ import annotations

from cabal.agents.llm_persona import LLMPersona
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.session_tracker import NoOpSessionTracker, SessionTracker
from cabal.core.memory.store import MemoryStore, NoOpMemoryStore

# Templates, not literals: every primus owns its own subordinates, so the
# owning primus's name is substituted at construction (see `Subordinate`).
# Both the agent NAME and the prompt used to hard-code "rook"/"Rook", which
# meant a second primus (jackdaw) got subordinates called `rook-implementer`
# that were told they worked for Rook — wrong bulletin/event/trace names and a
# wrong system prompt.
#
# `{primus}` is filled via str.format, so any LITERAL brace added to a prompt
# below must be doubled ({{ }}).
SUBORDINATE_PROMPTS = {
    "implementer": (
        "You are {primus}'s implementer subordinate. The primus has handed you "
        "a discrete, well-scoped engineering task and the relevant context. "
        "Carry it out — produce code, diffs, or concrete next steps. Do not "
        "engage in further planning; that's the primus's job."
    ),
    "reviewer": (
        "You are {primus}'s reviewer subordinate. The primus is showing you the "
        "implementer's output. Find real problems — correctness, security, "
        "edge cases the implementer missed, race conditions, broken "
        "invariants. Be specific. Skip generic style critique."
    ),
}


def _display(primus: str) -> str:
    """Config keys are lowercase (`rook`, `jackdaw`); prompts address the primus
    by name. Upper-cases the first character only, so an intentionally-cased
    name (`mcCarthy`) isn't flattened the way str.capitalize() would."""
    return primus[:1].upper() + primus[1:]


class Subordinate(LLMPersona):
    """LLMPersona with a fixed role-derived system prompt, owned by one primus."""

    def __init__(
        self,
        primus: str,
        role: str,
        llm_client: LiteLLMClient,
        model: str,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
        memory_store: MemoryStore | None = None,
        session_tracker: SessionTracker | None = None,
    ) -> None:
        # `primus` is REQUIRED rather than defaulted: a default is exactly how
        # the previous bug stayed invisible — every caller silently inherited
        # one primus's identity. Callers must say who owns the subordinate.
        template = SUBORDINATE_PROMPTS.get(role, "You are {primus}'s {role} subordinate.")
        super().__init__(
            name=f"{primus}-{role}",
            model=model,
            llm_client=llm_client,
            system_prompt=system_prompt
            or template.format(primus=_display(primus), role=role),
            max_tokens=max_tokens,
            memory_store=memory_store or NoOpMemoryStore(),
            session_tracker=session_tracker or NoOpSessionTracker(),
        )
