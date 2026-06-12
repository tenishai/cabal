"""LLMClient Protocol tests.

Verifies the default LiteLLMClient impl satisfies the Protocol and that
both deployment shapes (library mode + proxy mode via api_base) work
through the same code path.
"""

from __future__ import annotations

import pytest

from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.llm.protocol import LLMClient


@pytest.fixture
def hook_registry() -> HookRegistry:
    return HookRegistry()


@pytest.fixture
def captured_calls() -> list[dict]:
    return []


def _make_stub_acompletion(captured: list[dict]):
    """Stub acompletion_fn that records kwargs and returns a canned response."""

    async def stub(*, model, messages, **kwargs):
        captured.append({"model": model, "messages": messages, **kwargs})
        return {
            "choices": [{"message": {"content": "stub response"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    return stub


def test_litellm_client_satisfies_protocol(hook_registry) -> None:
    """LiteLLMClient duck-types to LLMClient at runtime."""
    captured: list[dict] = []
    client = LiteLLMClient(
        acompletion_fn=_make_stub_acompletion(captured),
        hook_registry=hook_registry,
    )
    assert isinstance(client, LLMClient)


@pytest.mark.asyncio
async def test_library_mode_no_api_base(hook_registry, captured_calls) -> None:
    """Library mode: no api_base / api_key — call goes through as-is."""
    client = LiteLLMClient(
        acompletion_fn=_make_stub_acompletion(captured_calls),
        hook_registry=hook_registry,
    )
    response = await client.acompletion(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert response["choices"][0]["message"]["content"] == "stub response"
    assert len(captured_calls) == 1
    assert "api_base" not in captured_calls[0]
    assert "api_key" not in captured_calls[0]


@pytest.mark.asyncio
async def test_proxy_mode_threads_api_base_and_key(hook_registry, captured_calls) -> None:
    """Proxy mode: api_base + api_key forwarded into the underlying call.

    The LiteLLM library handles the actual proxy hop — cabal just sets the
    config. From cabal's perspective, both modes are the same code path.
    """
    client = LiteLLMClient(
        acompletion_fn=_make_stub_acompletion(captured_calls),
        hook_registry=hook_registry,
        api_base="http://litellm:4000",
        api_key="sk-proxy-token-xyz",
    )
    await client.acompletion(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert captured_calls[0]["api_base"] == "http://litellm:4000"
    assert captured_calls[0]["api_key"] == "sk-proxy-token-xyz"


@pytest.mark.asyncio
async def test_caller_can_override_api_base_per_call(hook_registry, captured_calls) -> None:
    """Per-call api_base wins over the client-level default.

    Useful when a specific Task wants to bypass the default proxy and hit
    a different endpoint (e.g., a local Ollama for a privacy-sensitive
    persona).
    """
    client = LiteLLMClient(
        acompletion_fn=_make_stub_acompletion(captured_calls),
        hook_registry=hook_registry,
        api_base="http://litellm:4000",
    )
    await client.acompletion(
        model="ollama/llama3",
        messages=[{"role": "user", "content": "hi"}],
        api_base="http://ollama:11434",
    )
    assert captured_calls[0]["api_base"] == "http://ollama:11434"


@pytest.mark.asyncio
async def test_streaming_satisfies_protocol_shape(hook_registry) -> None:
    """astream returns an async iterator of text deltas."""

    async def stub_stream(*, model, messages, **kwargs):
        async def gen():
            for chunk in ["hel", "lo", " world"]:
                yield {"choices": [{"delta": {"content": chunk}}]}

        return gen()

    client = LiteLLMClient(
        acompletion_fn=stub_stream,
        hook_registry=hook_registry,
    )
    chunks = []
    async for delta in client.astream(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(delta)
    assert "".join(chunks) == "hello world"
