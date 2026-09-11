"""Archivist — memory + search singleton.

System prompt distilled from cabal.md's Archivist role: answers questions
about prior conversations and stored artifacts. Memory backend wiring
lands in Phase 3 (techne); for Phase 2 the persona runs as a plain LLM
agent with a memory-aware system prompt.
"""

from __future__ import annotations

from cabal.agents.llm_persona import LLMPersona
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.session_tracker import NoOpSessionTracker, SessionTracker
from cabal.core.memory.store import MemoryStore, NoOpMemoryStore

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
the right destination: Mason for engineering, Architect for patterns,
Librarian for external documentation.
"""


DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"


class ArchivistPersona(LLMPersona):
    def __init__(
        self,
        llm_client: LiteLLMClient,
        model: str | None = None,
        max_tokens: int = 8192,
        memory_store: MemoryStore | None = None,
        session_tracker: SessionTracker | None = None,
        commit_to_memory: bool = False,
    ) -> None:
        super().__init__(
            name="archivist",
            model=model or DEFAULT_MODEL,
            llm_client=llm_client,
            system_prompt=ARCHIVIST_PROMPT,
            max_tokens=max_tokens,
            memory_store=memory_store or NoOpMemoryStore(),
            session_tracker=session_tracker or NoOpSessionTracker(),
            # OFF BY DEFAULT, and the reasoning inverted (cabal#139). This read
            # "Archivist is THE memory persona -- accumulate by default", which
            # is true of curated memory and wrong for the persona's own output.
            #
            # Every other persona writing to memory pollutes a store SOMEONE
            # ELSE reads. The Archivist pollutes the store IT ITSELF searches,
            # so its answer is recallable on the next dispatch and can be
            # re-cited by the same agent -- a feedback loop with nothing
            # STRUCTURAL marking model output as distinct from curated
            # knowledge. (The `[archivist]` prefix and the `archivist` tag DO
            # reach the prompt, deliberately -- llm_persona documents the prefix
            # as existing "so search results stay attributable". But they are
            # advisory text a model may ignore; MemoryHit has no source field,
            # so nothing filters, ranks or gates on provenance.) REPORTED IN
            # cabal#141, not re-measured here: a recall returned two hits, both
            # the agent's own prior answers, both carrying the same fabricated
            # citation, out of a store of 1,118 curated rows.
            #
            # It is also the persona least able to afford the write, because
            # these rows land as type="fact", confidence="medium" -- the same
            # standing as a human-curated row. Not merely unlabelled: asserted.
            #
            # THIS IS THE BLUNT HALF OF THE FIX. cabal#141 is the sharp one --
            # `commit_to_memory` should mean PROPOSE rather than ASSERT, landing
            # rows as techne's `uncertain` status so they are excluded from
            # recall by default but still retrievable on request. That needs a
            # techne API change first: knowledge.store() writes no status column
            # at all. When #141 lands, this default can reasonably flip back,
            # because the harm will be in the write's SEMANTICS rather than in
            # the write happening.
            commit_to_memory=commit_to_memory,
            memory_commit_type="fact",
            memory_commit_confidence="medium",
        )
