"""Persona registry + concrete persona tests."""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.personas import (
    PERSONA_REGISTRY,
    ArchitectPersona,
    ArchivistPersona,
    LibrarianPersona,
)
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _user_task(text: str = "what do you remember?") -> Task:
    return Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _stub_client(reply: str = "ok") -> LiteLLMClient:
    async def fn(model: str, messages: list[dict[str, Any]], **kwargs):
        return {"choices": [{"message": {"content": reply}}], "model": model}

    return LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())


def test_registry_contains_all_known_personas() -> None:
    assert set(PERSONA_REGISTRY.keys()) == {
        "archivist",
        "architect",
        "wright",
        "librarian",
        "mason",
    }


@pytest.mark.asyncio
async def test_archivist_system_prompt_includes_memory_role() -> None:
    """Verify the right system prompt reaches the LLM."""
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list, **kwargs):
        seen["messages"] = messages
        seen["model"] = model
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    archivist = ArchivistPersona(client)
    await archivist.handle_task(_user_task())
    sys_msg = seen["messages"][0]
    assert sys_msg["role"] == "system"
    assert "Archivist" in sys_msg["content"]
    assert "memory" in sys_msg["content"].lower()


@pytest.mark.asyncio
async def test_architect_uses_opus_by_default() -> None:
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list, **kwargs):
        seen["model"] = model
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    architect = ArchitectPersona(client)
    await architect.handle_task(_user_task())
    assert "opus" in seen["model"].lower()


@pytest.mark.asyncio
async def test_librarian_system_prompt_emphasizes_citations() -> None:
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list, **kwargs):
        seen["messages"] = messages
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    librarian = LibrarianPersona(client)
    await librarian.handle_task(_user_task())
    sys_content = seen["messages"][0]["content"]
    assert "Cite" in sys_content or "cite" in sys_content


@pytest.mark.asyncio
async def test_persona_returns_completed_task() -> None:
    client = _stub_client("I remember that.")
    archivist = ArchivistPersona(client)
    response = await archivist.handle_task(_user_task())
    assert response.status.state == "completed"
    assert response.status.message.parts[0].text == "I remember that."


@pytest.mark.asyncio
async def test_model_override_takes_effect() -> None:
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list, **kwargs):
        seen["model"] = model
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    archivist = ArchivistPersona(client, model="custom/model-x")
    await archivist.handle_task(_user_task())
    assert seen["model"] == "custom/model-x"
