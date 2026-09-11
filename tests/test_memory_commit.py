"""Phase W: personas commit Q&A to memory after successful dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cabal.agents.llm_persona import LLMPersona
from cabal.agents.personas import ArchitectPersona, ArchivistPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.store import MemoryHit


@dataclass
class RecordingStore:
    commits: list[dict[str, Any]] = field(default_factory=list)

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        return []

    def commit(
        self, content: str, *, type: str = "fact", tags: str = "", confidence: str = "medium"
    ) -> None:
        self.commits.append(
            {"content": content, "type": type, "tags": tags, "confidence": confidence}
        )


def _stub(reply: str = "ok"):
    async def fn(model: str, messages: list, **kwargs):
        return {"choices": [{"message": {"content": reply}}]}

    return fn


def _task(text: str = "what's your name?") -> Task:
    return Task(
        id="t-commit",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


@pytest.mark.asyncio
async def test_default_persona_does_not_commit() -> None:
    """Base LLMPersona with no flag set must NOT commit."""
    store = RecordingStore()
    client = LiteLLMClient(acompletion_fn=_stub("hi"), hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x", model="x", llm_client=client, system_prompt="x", memory_store=store
    )
    await persona.handle_task(_task())
    assert store.commits == []


@pytest.mark.asyncio
async def test_commit_flag_persists_qa() -> None:
    store = RecordingStore()
    client = LiteLLMClient(
        acompletion_fn=_stub("I am the Archivist."), hook_registry=HookRegistry()
    )
    persona = LLMPersona(
        name="archivist",
        model="x",
        llm_client=client,
        system_prompt="x",
        memory_store=store,
        commit_to_memory=True,
    )
    await persona.handle_task(_task("what's your name?"))
    assert len(store.commits) == 1
    commit = store.commits[0]
    assert "what's your name?" in commit["content"]
    assert "I am the Archivist." in commit["content"]
    assert "[archivist]" in commit["content"]
    assert commit["tags"] == "archivist"


@pytest.mark.asyncio
async def test_archivist_does_not_commit_by_default() -> None:
    """cabal#139 — the Archivist must not write its own answers into the store
    it searches unless an operator asks for it.

    This test asserted the OPPOSITE until the default flipped, and inverting it
    is the point rather than collateral: the old behaviour meant one dispatch
    put a `[archivist] Q: ... A: ...` row, typed `fact` at `medium` confidence,
    into the very store the next dispatch recalls from. Reported in
    cabal#141 from the incident behind cabal#138 -- inherited, not re-measured
    by this change: a recall returned two hits, both the agent's own prior
    answers, both repeating the same fabricated citation, from a store of 1,118
    curated rows.

    The assertion is on the ABSENCE of a write, which is the property #139 is
    about. Asserting the flag's value instead would pass even if some other
    path committed.
    """
    store = RecordingStore()
    client = LiteLLMClient(
        acompletion_fn=_stub("memory answer"), hook_registry=HookRegistry()
    )
    archivist = ArchivistPersona(client, memory_store=store)
    await archivist.handle_task(_task())
    assert store.commits == [], (
        "the Archivist wrote its own answer into the store it searches; "
        "that is the feedback loop cabal#139 closes"
    )


@pytest.mark.asyncio
async def test_an_operator_can_still_turn_archivist_accumulation_on() -> None:
    """The default is safe to change precisely BECAUSE the knob survives.

    An accumulating Archivist is a legitimate deployment choice; what was wrong
    was making it the default on the one persona that reads what it writes. So
    the opt-in has to keep working, and it has to be tested in the direction
    that now matters.
    """
    store = RecordingStore()
    client = LiteLLMClient(
        acompletion_fn=_stub("memory answer"), hook_registry=HookRegistry()
    )
    archivist = ArchivistPersona(client, memory_store=store, commit_to_memory=True)
    await archivist.handle_task(_task())
    assert len(store.commits) == 1


@pytest.mark.asyncio
async def test_architect_does_not_commit_by_default() -> None:
    """ArchitectPersona stays opt-in for now — pattern accumulation comes later."""
    store = RecordingStore()
    client = LiteLLMClient(
        acompletion_fn=_stub("design answer"), hook_registry=HookRegistry()
    )
    architect = ArchitectPersona(client, memory_store=store)
    await architect.handle_task(_task())
    assert store.commits == []


@pytest.mark.asyncio
async def test_empty_or_failed_response_does_not_commit() -> None:
    """No agent text → no commit."""
    store = RecordingStore()
    client = LiteLLMClient(acompletion_fn=_stub("   "), hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x",
        model="x",
        llm_client=client,
        system_prompt="x",
        memory_store=store,
        commit_to_memory=True,
    )
    await persona.handle_task(_task())
    assert store.commits == []


@pytest.mark.asyncio
async def test_commit_truncated_to_max_chars() -> None:
    store = RecordingStore()
    long_reply = "x" * 5000
    client = LiteLLMClient(
        acompletion_fn=_stub(long_reply), hook_registry=HookRegistry()
    )
    persona = LLMPersona(
        name="big",
        model="x",
        llm_client=client,
        system_prompt="x",
        memory_store=store,
        commit_to_memory=True,
        memory_commit_max_chars=500,
    )
    await persona.handle_task(_task())
    assert len(store.commits) == 1
    assert len(store.commits[0]["content"]) <= 500


@pytest.mark.asyncio
async def test_commit_failure_swallowed() -> None:
    """A failing memory_store.commit must not break the dispatch."""

    @dataclass
    class BoomStore:
        def recall(self, query, *, limit=5):
            return []

        def commit(self, content, **kwargs):
            raise RuntimeError("disk full")

    client = LiteLLMClient(acompletion_fn=_stub("ok"), hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x",
        model="x",
        llm_client=client,
        system_prompt="x",
        memory_store=BoomStore(),
        commit_to_memory=True,
    )
    response = await persona.handle_task(_task())
    assert response.status.state == "completed"


@pytest.mark.asyncio
async def test_streaming_path_also_commits() -> None:
    """The astream_task path should also commit on completion."""
    store = RecordingStore()

    async def fn(model: str, messages: list, **kwargs):
        async def aiter():
            for ch in ["the ", "streamed ", "answer"]:
                yield {"choices": [{"delta": {"content": ch}}]}

        return aiter()

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="streamer",
        model="x",
        llm_client=client,
        system_prompt="x",
        memory_store=store,
        commit_to_memory=True,
    )
    deltas: list[str] = []
    async for d in persona.astream_task(_task("ask something")):
        deltas.append(d)
    assert "".join(deltas) == "the streamed answer"
    assert len(store.commits) == 1
    assert "the streamed answer" in store.commits[0]["content"]
