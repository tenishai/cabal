"""Librarian — external-documentation lookup singleton.

System prompt for fetching, summarizing, and citing external docs.
Phase 1 answers from training data; Phase 2 adds WebFetch via limenal so
Librarian's answers carry real citations and current information.
"""

from __future__ import annotations

from cabal.agents.llm_persona import LLMPersona
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.session_tracker import NoOpSessionTracker, SessionTracker
from cabal.core.memory.store import MemoryStore, NoOpMemoryStore


LIBRARIAN_PROMPT = """\
You are Cabal's Librarian: external-documentation lookup singleton.

Your role:
- Surface authoritative docs for the libraries, protocols, and tools the
  operator's task touches.
- Cite — every claim includes a source (doc URL, RFC number, repo file).
- When something is current-events-dependent (a library version, an API
  shape that changed recently), flag it as needing verification rather
  than asserting from training data.

What you are NOT:
- A summarizer of the operator's own files (that's Archivist).
- A pattern-recommender (that's Architect).
- A code-writer (that's Rook).
"""


DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"


class LibrarianPersona(LLMPersona):
    def __init__(
        self,
        llm_client: LiteLLMClient,
        model: str | None = None,
        max_tokens: int = 8192,
        memory_store: MemoryStore | None = None,
        session_tracker: SessionTracker | None = None,
    ) -> None:
        super().__init__(
            name="librarian",
            model=model or DEFAULT_MODEL,
            llm_client=llm_client,
            system_prompt=LIBRARIAN_PROMPT,
            max_tokens=max_tokens,
            memory_store=memory_store or NoOpMemoryStore(),
            session_tracker=session_tracker or NoOpSessionTracker(),
        )
