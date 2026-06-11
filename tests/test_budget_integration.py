"""End-to-end: BudgetGuard wired into LiteLLMClient + LLMPersona."""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.codes import CODE_BUDGET_EXHAUSTED, reason_code
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.budget import BudgetGuard
from cabal.core.llm.litellm_client import LiteLLMClient, LLMUnavailableError


def _task(task_id: str = "t-1", text: str = "hi") -> Task:
    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _stub(reply: str = "ok", *, prompt_tokens: int = 100, completion_tokens: int = 50):
    async def fn(model: str, messages: list, **kwargs):
        return {
            "choices": [{"message": {"content": reply}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    return fn


@pytest.mark.asyncio
async def test_acompletion_reserves_and_refunds_budget() -> None:
    guard = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    client = LiteLLMClient(
        acompletion_fn=_stub("x", prompt_tokens=400, completion_tokens=200),
        hook_registry=HookRegistry(),
        budget=guard,
    )
    await client.acompletion(model="x", messages=[], task_id="t-budget")
    # Reserved 10_000; used 600; refunded 9_400; net used = 600
    assert guard.remaining("t-budget") == 100_000 - 600


@pytest.mark.asyncio
async def test_acompletion_without_task_id_skips_budget() -> None:
    """No task_id → budget is not consulted (existing tests assume this)."""
    guard = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    client = LiteLLMClient(
        acompletion_fn=_stub("x"),
        hook_registry=HookRegistry(),
        budget=guard,
    )
    await client.acompletion(model="x", messages=[])
    # No task tracked
    assert guard.remaining("t-anything") == 100_000


@pytest.mark.asyncio
async def test_acompletion_failure_refunds_full_reservation() -> None:
    guard = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)

    async def boom(model: str, messages: list, **kwargs):
        raise type("APIConnectionError", (Exception,), {})("nope")

    client = LiteLLMClient(
        acompletion_fn=boom,
        hook_registry=HookRegistry(),
        budget=guard,
    )
    with pytest.raises(Exception):  # LLMUnavailableError
        await client.acompletion(model="x", messages=[], task_id="t-fail")
    # Reservation fully refunded
    assert guard.remaining("t-fail") == 100_000


@pytest.mark.asyncio
async def test_budget_exhausted_translated_to_llm_unavailable() -> None:
    guard = BudgetGuard(default_budget=5_000, reserve_per_dispatch=10_000)
    client = LiteLLMClient(
        acompletion_fn=_stub("x"),
        hook_registry=HookRegistry(),
        budget=guard,
    )
    with pytest.raises(LLMUnavailableError, match="need 10000"):
        await client.acompletion(model="x", messages=[], task_id="t-broke")


@pytest.mark.asyncio
async def test_llm_persona_returns_702_on_budget_exhaustion() -> None:
    """A persona whose budget can't fit a reservation should return a Task
    annotated with the 702 reason code, not raise."""
    guard = BudgetGuard(default_budget=5_000, reserve_per_dispatch=10_000)
    client = LiteLLMClient(
        acompletion_fn=_stub("x"),
        hook_registry=HookRegistry(),
        budget=guard,
    )
    persona = LLMPersona(
        name="echo",
        model="x",
        llm_client=client,
        system_prompt="x",
    )
    response = await persona.handle_task(_task())
    assert response.status.state == "failed"
    assert reason_code(response) == CODE_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_cabal_token_budget_metadata_override() -> None:
    """task.metadata.cabal_token_budget overrides the default."""
    guard = BudgetGuard(default_budget=200_000, reserve_per_dispatch=10_000)
    client = LiteLLMClient(
        acompletion_fn=_stub("x"),
        hook_registry=HookRegistry(),
        budget=guard,
    )
    persona = LLMPersona(name="x", model="x", llm_client=client, system_prompt="x")
    task = _task("t-override")
    task.metadata["cabal_token_budget"] = 8_000  # less than RESERVE_PER_DISPATCH
    response = await persona.handle_task(task)
    # Budget can't cover the reservation → 702
    assert response.status.state == "failed"
    assert reason_code(response) == CODE_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_circuit_open_refunds_budget() -> None:
    """If the breaker fires after reserving, the reservation should be refunded."""
    from cabal.core.llm.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1)
    # Pre-trip the breaker
    breaker.before_call()
    breaker.on_failure()
    assert breaker.state.value == "open"

    guard = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    client = LiteLLMClient(
        acompletion_fn=_stub("x"),
        hook_registry=HookRegistry(),
        budget=guard,
        breaker=breaker,
    )
    with pytest.raises(LLMUnavailableError, match="circuit OPEN"):
        await client.acompletion(model="x", messages=[], task_id="t-circuit")
    # Reservation refunded despite the breaker firing
    assert guard.remaining("t-circuit") == 100_000
