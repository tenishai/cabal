"""LiteLLM wrapper tests."""

from __future__ import annotations

import pytest

from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.circuit_breaker import CircuitBreaker, CircuitState
from cabal.core.llm.litellm_client import (
    CabalLLMError,
    LiteLLMClient,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMUnavailableError,
)


# Stand-in exception types matching litellm's actual class names
class APIConnectionError(Exception):
    pass


class RateLimitError(Exception):
    pass


class BadRequestError(Exception):
    pass


@pytest.fixture
def hook_registry() -> HookRegistry:
    return HookRegistry()


@pytest.mark.asyncio
async def test_successful_call_returns_response(hook_registry) -> None:
    async def fake(model: str, messages: list, **kwargs):
        return {"choices": [{"message": {"content": "ok"}}], "model": model}

    client = LiteLLMClient(acompletion_fn=fake, hook_registry=hook_registry)
    resp = await client.acompletion(
        model="anthropic/claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp["choices"][0]["message"]["content"] == "ok"
    assert client.breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_pre_and_post_hooks_fire_on_success(hook_registry) -> None:
    pre_calls: list[dict] = []
    post_calls: list[dict] = []
    hook_registry.register(HookName.LITELLM_PRE, lambda p: pre_calls.append(p))
    hook_registry.register(HookName.LITELLM_POST, lambda p: post_calls.append(p))

    async def fake(**kwargs):
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(acompletion_fn=fake, hook_registry=hook_registry)
    await client.acompletion(model="x", messages=[])

    assert len(pre_calls) == 1
    assert len(post_calls) == 1
    assert post_calls[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_rate_limit_error_classified(hook_registry) -> None:
    async def fake(**kwargs):
        raise RateLimitError("slow down")

    client = LiteLLMClient(acompletion_fn=fake, hook_registry=hook_registry)
    with pytest.raises(LLMRateLimitedError):
        await client.acompletion(model="x", messages=[])


@pytest.mark.asyncio
async def test_connection_error_classified(hook_registry) -> None:
    async def fake(**kwargs):
        raise APIConnectionError("proxy down")

    client = LiteLLMClient(acompletion_fn=fake, hook_registry=hook_registry)
    with pytest.raises(LLMUnavailableError):
        await client.acompletion(model="x", messages=[])


@pytest.mark.asyncio
async def test_bad_request_classified(hook_registry) -> None:
    async def fake(**kwargs):
        raise BadRequestError("unknown model")

    client = LiteLLMClient(acompletion_fn=fake, hook_registry=hook_registry)
    with pytest.raises(LLMInvalidRequestError):
        await client.acompletion(model="x", messages=[])


@pytest.mark.asyncio
async def test_unknown_error_becomes_base_class(hook_registry) -> None:
    async def fake(**kwargs):
        raise RuntimeError("¯\\_(ツ)_/¯")

    client = LiteLLMClient(acompletion_fn=fake, hook_registry=hook_registry)
    with pytest.raises(CabalLLMError):
        await client.acompletion(model="x", messages=[])


@pytest.mark.asyncio
async def test_failures_trip_breaker_then_block_further_calls(hook_registry) -> None:
    """Three connection errors → breaker OPEN → next call fast-fails."""

    async def fake(**kwargs):
        raise APIConnectionError("down")

    client = LiteLLMClient(
        acompletion_fn=fake,
        hook_registry=hook_registry,
        breaker=CircuitBreaker(failure_threshold=3),
    )
    for _ in range(3):
        with pytest.raises(LLMUnavailableError):
            await client.acompletion(model="x", messages=[])

    assert client.breaker.state == CircuitState.OPEN
    # 4th call: blocked WITHOUT calling acompletion_fn at all
    call_count = {"n": 0}

    async def counting_fake(**kwargs):
        call_count["n"] += 1
        raise APIConnectionError("still down")

    client.acompletion_fn = counting_fake
    with pytest.raises(LLMUnavailableError, match="circuit OPEN"):
        await client.acompletion(model="x", messages=[])
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_post_hook_carries_outcome_on_error(hook_registry) -> None:
    post_calls: list[dict] = []
    hook_registry.register(HookName.LITELLM_POST, lambda p: post_calls.append(p))

    async def fake(**kwargs):
        raise APIConnectionError("down")

    client = LiteLLMClient(acompletion_fn=fake, hook_registry=hook_registry)
    with pytest.raises(LLMUnavailableError):
        await client.acompletion(model="x", messages=[])

    assert post_calls[-1]["outcome"] == "error"
    assert post_calls[-1]["error_type"] == "APIConnectionError"


@pytest.mark.asyncio
async def test_api_base_and_key_are_passed_through(hook_registry) -> None:
    seen: dict = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return {"choices": [{"message": {"content": "x"}}]}

    client = LiteLLMClient(
        acompletion_fn=fake,
        hook_registry=hook_registry,
        api_base="http://litellm:4000",
        api_key="sk-test",
    )
    await client.acompletion(model="x", messages=[])
    assert seen["api_base"] == "http://litellm:4000"
    assert seen["api_key"] == "sk-test"
