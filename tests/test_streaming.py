"""A2A message/stream + LiteLLMClient.astream + LLMPersona.astream_task."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient, LLMUnavailableError
from cabal.core.models import BootstrapCode


def _chunks(*texts: str) -> Any:
    """Build an async-iterable streaming response from text chunks."""

    async def aiter():
        for t in texts:
            yield {"choices": [{"delta": {"content": t}}]}

    return aiter()


def _streaming_stub(*chunks: str):
    """Returns an acompletion_fn that, when called with stream=True, returns
    an async iterator yielding the supplied text chunks."""

    async def fn(model: str, messages: list, **kwargs):
        assert kwargs.get("stream") is True
        return _chunks(*chunks)

    return fn


# ---- LiteLLMClient.astream -------------------------------------------------


@pytest.mark.asyncio
async def test_astream_yields_text_deltas() -> None:
    client = LiteLLMClient(
        acompletion_fn=_streaming_stub("Hello ", "world", "!"),
        hook_registry=HookRegistry(),
    )
    out: list[str] = []
    async for delta in client.astream(model="x", messages=[]):
        out.append(delta)
    assert out == ["Hello ", "world", "!"]


@pytest.mark.asyncio
async def test_astream_empty_chunks_are_skipped() -> None:
    """Chunks with no delta.content (heartbeats, role-only first chunk) skip."""

    async def fn(model: str, messages: list, **kwargs):
        async def aiter():
            yield {"choices": [{"delta": {"role": "assistant"}}]}  # role only
            yield {"choices": [{"delta": {"content": "real text"}}]}
            yield {"choices": [{"delta": {}}]}  # empty

        return aiter()

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    out: list[str] = []
    async for delta in client.astream(model="x", messages=[]):
        out.append(delta)
    assert out == ["real text"]


@pytest.mark.asyncio
async def test_astream_fires_pre_and_post_hooks() -> None:
    from cabal.core.hooks.registry import HookName

    reg = HookRegistry()
    pre_calls: list[dict] = []
    post_calls: list[dict] = []
    reg.register(HookName.LITELLM_PRE, lambda p: pre_calls.append(p))
    reg.register(HookName.LITELLM_POST, lambda p: post_calls.append(p))

    client = LiteLLMClient(
        acompletion_fn=_streaming_stub("a", "b"), hook_registry=reg
    )
    async for _ in client.astream(model="x", messages=[]):
        pass

    assert len(pre_calls) == 1
    assert pre_calls[0]["stream"] is True
    assert len(post_calls) == 1
    assert post_calls[0]["outcome"] == "success"
    assert post_calls[0]["full_text"] == "ab"


@pytest.mark.asyncio
async def test_astream_failure_classified_and_breaker_trips() -> None:
    """Network failure mid-stream → classified + breaker counts the failure."""
    from cabal.core.llm.circuit_breaker import CircuitBreaker

    async def fn(model: str, messages: list, **kwargs):
        async def aiter():
            yield {"choices": [{"delta": {"content": "partial"}}]}
            raise type("APIConnectionError", (Exception,), {})("net dropped")

        return aiter()

    breaker = CircuitBreaker(failure_threshold=1)
    client = LiteLLMClient(
        acompletion_fn=fn, hook_registry=HookRegistry(), breaker=breaker
    )
    collected: list[str] = []
    with pytest.raises(LLMUnavailableError):
        async for delta in client.astream(model="x", messages=[]):
            collected.append(delta)
    assert collected == ["partial"]
    assert breaker.state.value == "open"


# ---- LLMPersona.astream_task -----------------------------------------------


@pytest.mark.asyncio
async def test_persona_astream_yields_deltas_and_calls_session() -> None:
    from dataclasses import dataclass, field

    @dataclass
    class RecTracker:
        starts: list[dict] = field(default_factory=list)
        ends: list[dict] = field(default_factory=list)

        def start(self, *, external_id: str, agent_identity: str) -> str:
            self.starts.append(
                {"external_id": external_id, "agent_identity": agent_identity}
            )
            return external_id

        def end(self, *, session_id: str, summary: str) -> None:
            self.ends.append({"session_id": session_id, "summary": summary})

    tracker = RecTracker()
    client = LiteLLMClient(
        acompletion_fn=_streaming_stub("Hello ", "operator"),
        hook_registry=HookRegistry(),
    )
    persona = LLMPersona(
        name="archivist",
        model="x",
        llm_client=client,
        system_prompt="x",
        session_tracker=tracker,
    )
    task = Task(
        id="t-stream",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="hi")]),
        ),
    )
    out: list[str] = []
    async for delta in persona.astream_task(task):
        out.append(delta)
    assert "".join(out) == "Hello operator"
    assert len(tracker.starts) == 1
    assert tracker.ends == [{"session_id": "t-stream", "summary": "Hello operator"}]


# ---- /agents/{name}/stream SSE endpoint ----------------------------------


async def _bootstrap_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_stream_endpoint_emits_chunks_and_status_update(
    app: FastAPI, client: AsyncClient
) -> None:
    """The echo agent doesn't implement astream_task, so we register a fake
    streaming handler before the test runs."""

    class FakeStreamer:
        name = "echo-stream"

        async def astream_task(self, task: Task) -> AsyncIterator[str]:
            yield "first "
            yield "second"

    app.state.dispatch_router.register_local(FakeStreamer())

    # Seed bulletin row so dispatch resolution doesn't 404
    from cabal.core.bulletin import store as bulletin_store
    from cabal.core.db import open_session
    from cabal.core.models import Persona

    async with open_session(app.state.sessionmaker) as s:
        s.add(Persona(name="echo-stream", config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name="echo-stream",
            persona_name="echo-stream",
            role="builtin",
            endpoint="local://echo-stream",
            capabilities={},
            safety_tier="strict",
            agent_card_json={"name": "echo-stream"},
            agent_card_kid=app.state.active_kid,
        )

    token = await _bootstrap_token(app, client)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/stream",
        "params": {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
        },
    }
    async with client.stream(
        "POST",
        "/agents/echo-stream/stream",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events: list[str] = []
        async for line in resp.aiter_lines():
            events.append(line)

    full = "\n".join(events)
    assert "event: chunk" in full
    assert '"delta": "first "' in full
    assert '"delta": "second"' in full
    assert "event: status-update" in full
    assert '"final": true' in full


@pytest.mark.asyncio
async def test_stream_endpoint_404_for_unknown_agent(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _bootstrap_token(app, client)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/stream",
        "params": {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "x"}]}
        },
    }
    async with client.stream(
        "POST",
        "/agents/ghost/stream",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        body_text = "".join([chunk async for chunk in resp.aiter_text()])
    assert "no in-process handler" in body_text or "agent_not_found" in body_text.lower() or "-32003" in body_text


@pytest.mark.asyncio
async def test_stream_endpoint_wrong_method_rejected(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _bootstrap_token(app, client)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}}
    async with client.stream(
        "POST",
        "/agents/echo/stream",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        body_text = "".join([chunk async for chunk in resp.aiter_text()])
    assert "message/stream" in body_text
