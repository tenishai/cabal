"""LLMPersona tests with a stubbed completion fn."""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.llm_persona import HONESTY_FLOOR, LLMPersona
from cabal.core.a2a.codes import (
    CODE_BUDGET_EXHAUSTED,
    CODE_POLICY_VIOLATION,
    reason_code,
)
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _user_task(text: str = "what is the capital of France?") -> Task:
    return Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _stub(reply: str):
    async def fn(model: str, messages: list[dict[str, Any]], **kwargs) -> dict:
        return {
            "choices": [{"message": {"role": "assistant", "content": reply}}],
            "model": model,
        }

    return fn


@pytest.mark.asyncio
async def test_llm_persona_returns_completed_task() -> None:
    client = LiteLLMClient(
        acompletion_fn=_stub("Paris."), hook_registry=HookRegistry()
    )
    persona = LLMPersona(
        name="geographer",
        model="anthropic/claude-sonnet-4-6",
        llm_client=client,
        system_prompt="You answer geography questions.",
    )
    task = _user_task()
    response = await persona.handle_task(task)
    assert response.status.state == "completed"
    assert response.status.message is not None
    assert response.status.message.parts[0].text == "Paris."
    # History should contain the user message
    assert any(m.role == "user" for m in response.history)


@pytest.mark.asyncio
async def test_build_messages_includes_system_prompt() -> None:
    """system_prompt is the first message in the chat array."""
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list[dict[str, Any]], **kwargs) -> dict:
        seen["messages"] = messages
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x",
        model="x",
        llm_client=client,
        system_prompt="be concise",
    )
    await persona.handle_task(_user_task("hi"))
    # The system message is the persona prompt PLUS the honesty floor every
    # prompt now carries (cabal#138), so this asserts the persona's own text
    # leads and survives rather than asserting the message equals it.
    assert seen["messages"][0]["role"] == "system"
    assert seen["messages"][0]["content"].startswith("be concise")
    assert HONESTY_FLOOR in seen["messages"][0]["content"]
    assert seen["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_history_replays_into_messages() -> None:
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list[dict[str, Any]], **kwargs) -> dict:
        seen["messages"] = messages
        return {"choices": [{"message": {"content": "ack"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x", model="x", llm_client=client, system_prompt="x"
    )
    task = Task(
        id="t",
        status=TaskStatus(
            state="working",
            message=Message(role="user", parts=[TextPart(text="latest")]),
        ),
        history=[
            Message(role="user", parts=[TextPart(text="first")]),
            Message(role="agent", parts=[TextPart(text="prior reply")]),
        ],
    )
    await persona.handle_task(task)
    msgs = seen["messages"]
    # system + history (2) + current = 4
    assert len(msgs) == 4
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "prior reply"
    assert msgs[3]["content"] == "latest"


@pytest.mark.asyncio
async def test_llm_unavailable_yields_702_reason_code() -> None:
    async def boom(model: str, messages: list, **kwargs) -> dict:
        # Match LLMUnavailableError sniff
        raise type("APIConnectionError", (Exception,), {})("proxy down")

    client = LiteLLMClient(acompletion_fn=boom, hook_registry=HookRegistry())
    persona = LLMPersona(name="x", model="x", llm_client=client)
    response = await persona.handle_task(_user_task())
    assert response.status.state == "failed"
    assert reason_code(response) == CODE_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_llm_bad_request_yields_701_reason_code() -> None:
    async def reject(model: str, messages: list, **kwargs) -> dict:
        raise type("BadRequestError", (Exception,), {})("unknown model")

    client = LiteLLMClient(acompletion_fn=reject, hook_registry=HookRegistry())
    persona = LLMPersona(name="x", model="x", llm_client=client)
    response = await persona.handle_task(_user_task())
    assert response.status.state == "failed"
    assert reason_code(response) == CODE_POLICY_VIOLATION


@pytest.mark.asyncio
async def test_max_tokens_passed_through() -> None:
    seen: dict[str, Any] = {}

    async def capture(model: str, messages: list, **kwargs) -> dict:
        seen.update(kwargs)
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x", model="x", llm_client=client, max_tokens=4096
    )
    await persona.handle_task(_user_task())
    assert seen["max_tokens"] == 4096
