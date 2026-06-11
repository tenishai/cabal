"""LLMPersona.handle_task wraps work in session_tracker.start/end."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _task(task_id: str = "t-sess", text: str = "hello") -> Task:
    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _stub(reply: str = "ok"):
    async def fn(model: str, messages: list, **kwargs):
        return {"choices": [{"message": {"content": reply}}]}

    return fn


@dataclass
class RecordingTracker:
    starts: list[dict[str, str]] = field(default_factory=list)
    ends: list[dict[str, str]] = field(default_factory=list)
    next_session_id: str = "session-from-tracker"

    def start(self, *, external_id: str, agent_identity: str) -> str:
        self.starts.append(
            {"external_id": external_id, "agent_identity": agent_identity}
        )
        return self.next_session_id

    def end(self, *, session_id: str, summary: str) -> None:
        self.ends.append({"session_id": session_id, "summary": summary})


@pytest.mark.asyncio
async def test_handle_task_starts_and_ends_session_on_success() -> None:
    tracker = RecordingTracker()
    client = LiteLLMClient(
        acompletion_fn=_stub("the reply"), hook_registry=HookRegistry()
    )
    persona = LLMPersona(
        name="archivist",
        model="x",
        llm_client=client,
        system_prompt="x",
        session_tracker=tracker,
    )
    await persona.handle_task(_task())

    assert tracker.starts == [
        {"external_id": "t-sess", "agent_identity": "archivist"}
    ]
    assert len(tracker.ends) == 1
    assert tracker.ends[0]["session_id"] == "session-from-tracker"
    assert tracker.ends[0]["summary"] == "the reply"


@pytest.mark.asyncio
async def test_handle_task_ends_session_on_llm_unavailable() -> None:
    """Session is closed even when LLM fails — summary records the error."""
    tracker = RecordingTracker()

    async def boom(model: str, messages: list, **kwargs):
        raise type("APIConnectionError", (Exception,), {})("proxy down")

    client = LiteLLMClient(acompletion_fn=boom, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="archivist",
        model="x",
        llm_client=client,
        system_prompt="x",
        session_tracker=tracker,
    )
    response = await persona.handle_task(_task())

    assert response.status.state == "failed"
    assert len(tracker.starts) == 1
    assert len(tracker.ends) == 1
    assert "[failed]" in tracker.ends[0]["summary"]
    assert "LLM unavailable" in tracker.ends[0]["summary"]


@pytest.mark.asyncio
async def test_default_tracker_is_noop_for_persona() -> None:
    """An LLMPersona built without a tracker still works (NoOp default)."""
    client = LiteLLMClient(acompletion_fn=_stub("x"), hook_registry=HookRegistry())
    persona = LLMPersona(name="x", model="x", llm_client=client, system_prompt="x")
    response = await persona.handle_task(_task())
    assert response.status.state == "completed"


@pytest.mark.asyncio
async def test_session_id_is_propagated_to_end_call() -> None:
    tracker = RecordingTracker()
    tracker.next_session_id = "explicit-session-abc-123"
    client = LiteLLMClient(acompletion_fn=_stub("done"), hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x",
        model="x",
        llm_client=client,
        system_prompt="x",
        session_tracker=tracker,
    )
    await persona.handle_task(_task())
    assert tracker.ends[0]["session_id"] == "explicit-session-abc-123"
