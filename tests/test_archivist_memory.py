"""Archivist + techne memory integration test.

Pre-seeds a techne knowledge DB with facts, dispatches a question through
Archivist, verifies that the system prompt sent to the LLM included the
relevant memory hit.
"""
from __future__ import annotations
import pytest
pytest.importorskip("techne")


from pathlib import Path
from typing import Any


from cabal.agents.personas import ArchivistPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.techne_store import TechneMemoryStore


@pytest.fixture
def memory_store(tmp_path: Path) -> TechneMemoryStore:
    import techne

    db = tmp_path / "archive.db"
    techne.init_db(db)
    store = TechneMemoryStore(db_path=db)
    # Pre-seed
    store.commit(
        "Operator's favorite editor is Neovim",
        type="fact",
        tags="preferences",
        confidence="high",
    )
    store.commit(
        "Operator chose Python over Go for new services",
        type="decision",
        tags="languages",
        confidence="high",
    )
    return store


@pytest.mark.asyncio
async def test_archivist_injects_memory_into_system_prompt(memory_store) -> None:
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list[dict[str, Any]], **kwargs):
        seen["messages"] = messages
        return {"choices": [{"message": {"content": "Recalled."}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    archivist = ArchivistPersona(client, memory_store=memory_store)

    task = Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(
                role="user",
                parts=[TextPart(text="what editor do I prefer?")],
            ),
        ),
    )
    await archivist.handle_task(task)

    sys_content = seen["messages"][0]["content"]
    assert "Archivist" in sys_content  # base prompt
    assert "Relevant memories" in sys_content
    assert "Neovim" in sys_content
    # Other facts about decisions also recalled (techne FTS hits multiple)
    assert "Recalled" not in sys_content  # reply isn't a memory


@pytest.mark.asyncio
async def test_archivist_without_memory_omits_memory_block(memory_store) -> None:
    """A NoOp store (default) means no memory block in system prompt."""
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list, **kwargs):
        seen["messages"] = messages
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    archivist = ArchivistPersona(client)  # no memory_store

    task = Task(
        id="t",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="hi")]),
        ),
    )
    await archivist.handle_task(task)

    sys_content = seen["messages"][0]["content"]
    assert "Archivist" in sys_content
    assert "Relevant memories" not in sys_content


@pytest.mark.asyncio
async def test_archivist_memory_recall_for_unknown_query(memory_store) -> None:
    """Recalling for a totally unrelated query should yield 0 hits and no block."""
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list, **kwargs):
        seen["messages"] = messages
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    archivist = ArchivistPersona(client, memory_store=memory_store)

    task = Task(
        id="t-x",
        status=TaskStatus(
            state="submitted",
            message=Message(
                role="user",
                parts=[TextPart(text="kangaroo platypus rhinoceros nonsense")],
            ),
        ),
    )
    await archivist.handle_task(task)

    sys_content = seen["messages"][0]["content"]
    assert "Relevant memories" not in sys_content
