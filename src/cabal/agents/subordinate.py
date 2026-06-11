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


SUBORDINATE_PROMPTS = {
    "implementer": (
        "You are Rook's implementer subordinate. The primus has handed you "
        "a discrete, well-scoped engineering task and the relevant context. "
        "Carry it out — produce code, diffs, or concrete next steps. Do not "
        "engage in further planning; that's the primus's job."
    ),
    "reviewer": (
        "You are Rook's reviewer subordinate. The primus is showing you the "
        "implementer's output. Find real problems — correctness, security, "
        "edge cases the implementer missed, race conditions, broken "
        "invariants. Be specific. Skip generic style critique."
    ),
}


class Subordinate(LLMPersona):
    """LLMPersona with a fixed role-derived system prompt."""

    def __init__(
        self,
        role: str,
        llm_client: LiteLLMClient,
        model: str,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
        memory_store: MemoryStore | None = None,
        session_tracker: SessionTracker | None = None,
    ) -> None:
        super().__init__(
            name=f"rook-{role}",
            model=model,
            llm_client=llm_client,
            system_prompt=system_prompt or SUBORDINATE_PROMPTS.get(
                role, f"You are Rook's {role} subordinate."
            ),
            max_tokens=max_tokens,
            memory_store=memory_store or NoOpMemoryStore(),
            session_tracker=session_tracker or NoOpSessionTracker(),
        )
