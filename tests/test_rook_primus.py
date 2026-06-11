"""Rook primus + Subordinate dispatch tests."""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.personas.rook import RookPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _client(responses: list[str]) -> tuple[LiteLLMClient, list[dict[str, Any]]]:
    """Stub LLM client that returns each text in `responses` in order.

    Also returns the captured-calls list for assertions.
    """
    calls: list[dict[str, Any]] = []
    iterator = iter(responses)

    async def fn(model: str, messages: list, **kwargs):
        text = next(iterator, "(stub exhausted)")
        calls.append({"model": model, "messages": messages, "agent": kwargs.get("agent")})
        return {"choices": [{"message": {"content": text}}]}

    return (
        LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry()),
        calls,
    )


def _task(text: str = "implement json parsing") -> Task:
    return Task(
        id="t-rook-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _subordinate(client: LiteLLMClient) -> Subordinate:
    return Subordinate(role="implementer", llm_client=client, model="anthropic/claude-sonnet-4-6")


@pytest.mark.asyncio
async def test_rook_dispatches_to_implementer_and_aggregates() -> None:
    """The primus PLAN → DISPATCH → AGGREGATE pipeline produces a final Task."""
    primus_client, primus_calls = _client(
        [
            "Plan: write a JSON parser in 50 lines",  # plan call
            "Final summary for operator: implementer wrote a parser.",  # aggregate
        ]
    )
    sub_client, sub_calls = _client(
        ["Here is the JSON parser code:\n```python\nimport json\n```"]
    )
    subordinates = {"implementer": _subordinate(sub_client)}
    rook = RookPrimusPersona(primus_client, subordinates=subordinates)

    response = await rook.handle_task(_task())
    assert response.status.state == "completed"
    assert "implementer wrote a parser" in response.status.message.parts[0].text

    # Two primus calls: plan + aggregate. agent name + trace get stripped
    # from kwargs before reaching the stub (they're hook-payload only), so
    # distinguish by which system prompt was sent.
    assert len(primus_calls) == 2
    assert "You are Cabal's Rook" in primus_calls[0]["messages"][0]["content"]
    assert "Summarize the result" in primus_calls[1]["messages"][0]["content"]

    # One subordinate call
    assert len(sub_calls) == 1


@pytest.mark.asyncio
async def test_subordinate_dispatch_fires_hooks() -> None:
    hook_registry = HookRegistry()
    dispatched: list[dict[str, Any]] = []
    returned: list[dict[str, Any]] = []
    hook_registry.register(
        HookName.SUBORDINATE_DISPATCHED, lambda p: dispatched.append(p)
    )
    hook_registry.register(
        HookName.SUBORDINATE_RETURNED, lambda p: returned.append(p)
    )

    primus_client, _ = _client(["plan", "summary"])
    sub_client, _ = _client(["implementer reply"])
    subordinates = {"implementer": _subordinate(sub_client)}
    rook = RookPrimusPersona(
        primus_client, subordinates=subordinates, hook_registry=hook_registry
    )

    await rook.handle_task(_task())

    assert len(dispatched) == 1
    assert dispatched[0]["role"] == "implementer"
    assert dispatched[0]["parent_task_id"] == "t-rook-1"

    assert len(returned) == 1
    assert returned[0]["state"] == "completed"


@pytest.mark.asyncio
async def test_subordinate_sub_task_inherits_trace_id() -> None:
    """The sub-Task carries the parent's cabal_trace_id but a fresh span."""
    primus_client, _ = _client(["plan", "summary"])

    captured_sub_task: dict[str, Any] = {}

    class CaptureSub(Subordinate):
        async def handle_task(self, task: Task) -> Task:
            captured_sub_task["metadata"] = dict(task.metadata)
            return await super().handle_task(task)

    sub_client, _ = _client(["implementer reply"])
    captured = CaptureSub(
        role="implementer", llm_client=sub_client, model="anthropic/claude-sonnet-4-6"
    )

    rook = RookPrimusPersona(primus_client, subordinates={"implementer": captured})
    task = _task()
    task.metadata["cabal_trace_id"] = "a" * 32
    task.metadata["cabal_span_id"] = "b" * 16
    await rook.handle_task(task)

    sub_meta = captured_sub_task["metadata"]
    assert sub_meta["cabal_trace_id"] == "a" * 32
    assert sub_meta["cabal_span_id"] != "b" * 16  # fresh span per hop
    assert sub_meta["cabal_parent_task"] == "t-rook-1"
    assert sub_meta["cabal_subordinate_role"] == "implementer"


@pytest.mark.asyncio
async def test_rook_requires_implementer() -> None:
    primus_client, _ = _client([])
    with pytest.raises(ValueError, match="implementer"):
        RookPrimusPersona(primus_client, subordinates={})


@pytest.mark.asyncio
async def test_plan_text_passed_to_aggregate_prompt() -> None:
    """The aggregate call's prompt should contain both the plan and the
    implementer's output, so the LLM can summarize them together."""
    primus_client, primus_calls = _client(
        ["the plan is to do X carefully", "summary"]
    )
    sub_client, _ = _client(["implementer output: did the X thing"])
    subordinates = {"implementer": _subordinate(sub_client)}
    rook = RookPrimusPersona(primus_client, subordinates=subordinates)
    await rook.handle_task(_task())

    aggregate_messages = primus_calls[1]["messages"]
    final_user = aggregate_messages[-1]["content"]
    assert "the plan is to do X carefully" in final_user
    assert "did the X thing" in final_user
