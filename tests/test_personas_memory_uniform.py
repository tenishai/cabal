"""Phase R: every persona accepts memory_store + session_tracker uniformly.

These tests verify the architectural property — that any persona can be
wired with the shared memory/session components, not just Archivist.
"""
from __future__ import annotations

import pytest

pytest.importorskip("techne")


from pathlib import Path
from typing import Any

from cabal.agents.personas import (
    ArchitectPersona,
    ArchivistPersona,
    LibrarianPersona,
)
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.techne_store import TechneMemoryStore


@pytest.fixture
def shared_memory(tmp_path: Path) -> TechneMemoryStore:
    import techne

    db = tmp_path / "shared.db"
    techne.init_db(db)
    store = TechneMemoryStore(db_path=db)
    # Seed two distinct memories tagged for different personas.
    # NB: personal-memory types only (fact/decision/evaluation/procedure) —
    # `pattern` belongs in the Architect store (architect.db), not here
    # (techne#137), and TechneMemoryStore.commit drops it with a warning.
    store.commit(
        "Prefer composition over inheritance",
        type="decision",
        tags="architect",
        confidence="high",
    )
    store.commit(
        "RFC 9110 supersedes RFC 7230-7235 for HTTP semantics",
        type="fact",
        tags="librarian",
        confidence="high",
    )
    return store


def _task(text: str) -> Task:
    return Task(
        id="t",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _capture_client() -> tuple[LiteLLMClient, dict[str, Any]]:
    seen: dict[str, Any] = {}

    async def fn(model: str, messages: list, **kwargs):
        seen["messages"] = messages
        return {"choices": [{"message": {"content": "ack"}}]}

    return LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry()), seen


@pytest.mark.asyncio
async def test_architect_accepts_memory_store(shared_memory) -> None:
    client, seen = _capture_client()
    architect = ArchitectPersona(client, memory_store=shared_memory)
    await architect.handle_task(_task("composition pattern question"))
    sys_content = seen["messages"][0]["content"]
    assert "Architect" in sys_content
    assert "Relevant memories" in sys_content
    assert "composition over inheritance" in sys_content


@pytest.mark.asyncio
async def test_librarian_accepts_memory_store(shared_memory) -> None:
    client, seen = _capture_client()
    librarian = LibrarianPersona(client, memory_store=shared_memory)
    await librarian.handle_task(_task("RFC HTTP semantics"))
    sys_content = seen["messages"][0]["content"]
    assert "Librarian" in sys_content
    assert "Relevant memories" in sys_content
    assert "RFC 9110" in sys_content


@pytest.mark.asyncio
async def test_archivist_still_works(shared_memory) -> None:
    """Phase Q behavior still holds — no regression in the original wiring."""
    client, seen = _capture_client()
    archivist = ArchivistPersona(client, memory_store=shared_memory)
    await archivist.handle_task(_task("RFC HTTP"))
    sys_content = seen["messages"][0]["content"]
    assert "Archivist" in sys_content
    assert "Relevant memories" in sys_content


@pytest.mark.asyncio
async def test_subordinate_accepts_memory_store(shared_memory) -> None:
    """Rook subordinates can also share the memory DB now."""
    client, seen = _capture_client()
    impl = Subordinate(
        role="implementer",
        llm_client=client,
        model="x",
        memory_store=shared_memory,
    )
    await impl.handle_task(_task("composition pattern"))
    sys_content = seen["messages"][0]["content"]
    assert "implementer" in sys_content
    assert "Relevant memories" in sys_content


@pytest.mark.asyncio
async def test_persona_with_no_store_has_no_memory_block() -> None:
    """The opt-in is real — default leaves the system prompt clean."""
    client, seen = _capture_client()
    architect = ArchitectPersona(client)  # no memory_store
    await architect.handle_task(_task("any question"))
    sys_content = seen["messages"][0]["content"]
    assert "Architect" in sys_content
    assert "Relevant memories" not in sys_content
