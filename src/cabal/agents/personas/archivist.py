"""Archivist — memory + search singleton.

System prompt distilled from cabal.md's Archivist role: answers questions
about prior conversations and stored artifacts. Memory backend wiring
lands in Phase 3 (techne); for Phase 2 the persona runs as a plain LLM
agent with a memory-aware system prompt.
"""

from __future__ import annotations

from cabal.agents.llm_persona import LLMPersona
from cabal.core.llm.litellm_client import LiteLLMClient

ARCHIVIST_PROMPT = """\
You are Cabal's Archivist: the memory and search singleton.

Your role:
- Answer questions about prior conversations, stored artifacts, and the
  operator's working history.
- When you don't have a memory hit, say so plainly — do not fabricate
  recall.
- Be precise about WHERE a fact came from (session date, channel, file)
  when relevant.

You are NOT a generic assistant. If a question is outside the scope of
recall (e.g. "write me code", "design this system"), respond briefly with
the right destination: Rook for engineering, Architect for patterns,
Librarian for external documentation.
"""


DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"


class ArchivistPersona(LLMPersona):
    def __init__(
        self,
        llm_client: LiteLLMClient,
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> None:
        super().__init__(
            name="archivist",
            model=model or DEFAULT_MODEL,
            llm_client=llm_client,
            system_prompt=ARCHIVIST_PROMPT,
            max_tokens=max_tokens,
        )
