"""Jackdaw primus dispatch tests.

Mirrors test_rook_primus.py style — exercises the PLAN→DISPATCH→AGGREGATE
pipeline through the JackdawPrimusPersona subclass.
"""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.personas.jackdaw import (
    DEFAULT_MODEL,
    JACKDAW_SYSTEM_PROMPT,
    JackdawPrimusPersona,
)
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _client(responses: list[str]) -> tuple[LiteLLMClient, list[dict[str, Any]]]:
    """Stub LLM client that returns each text in `responses` in order."""
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


def _task(text: str = "add JSON serialisation to the User model") -> Task:
    return Task(
        id="t-jackdaw-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _subordinate(client: LiteLLMClient, role: str = "implementer") -> Subordinate:
    return Subordinate(role=role, llm_client=client, model="anthropic/claude-sonnet-5")


@pytest.mark.asyncio
async def test_jackdaw_dispatches_to_implementer_and_aggregates() -> None:
    """PLAN → DISPATCH → AGGREGATE pipeline produces a final completed Task."""
    primus_client, primus_calls = _client(
        [
            "Plan: add a to_json() method to User that serialises all fields",
            "Final summary: implementer added to_json() to User.",
        ]
    )
    sub_client, sub_calls = _client(
        ["Here is the implementation:\n```python\ndef to_json(self): ...\n```"]
    )
    subordinates = {"implementer": _subordinate(sub_client)}
    jackdaw = JackdawPrimusPersona(primus_client, subordinates=subordinates)

    response = await jackdaw.handle_task(_task())
    assert response.status.state == "completed"
    assert "implementer added" in response.status.message.parts[0].text

    # Two primus calls: plan + aggregate
    assert len(primus_calls) == 2
    # Plan call uses the Jackdaw system prompt
    assert "Jackdaw" in primus_calls[0]["messages"][0]["content"]
    # Aggregate call uses the aggregate prompt (contains "Summarize the result")
    assert "Summarize the result" in primus_calls[1]["messages"][0]["content"]

    # One subordinate call
    assert len(sub_calls) == 1


@pytest.mark.asyncio
async def test_jackdaw_uses_sonnet_5_by_default() -> None:
    """JackdawPrimusPersona defaults to anthropic/claude-sonnet-5."""
    primus_client, primus_calls = _client(["plan", "summary"])
    sub_client, _ = _client(["impl"])
    jackdaw = JackdawPrimusPersona(
        primus_client, subordinates={"implementer": _subordinate(sub_client)}
    )

    assert jackdaw.model == DEFAULT_MODEL
    assert "sonnet-5" in jackdaw.model

    await jackdaw.handle_task(_task())
    assert primus_calls[0]["model"] == DEFAULT_MODEL


@pytest.mark.asyncio
async def test_jackdaw_system_prompt_frames_senior_engineering() -> None:
    """The system prompt must convey a senior-engineering planning role."""
    assert "senior" in JACKDAW_SYSTEM_PROMPT.lower()
    assert "engineer" in JACKDAW_SYSTEM_PROMPT.lower()
    # The primus delegates — it does not write code itself
    assert "delegate" in JACKDAW_SYSTEM_PROMPT.lower()


@pytest.mark.asyncio
async def test_jackdaw_requires_implementer() -> None:
    """Missing implementer subordinate → ValueError."""
    primus_client, _ = _client([])
    with pytest.raises(ValueError, match="implementer"):
        JackdawPrimusPersona(primus_client, subordinates={})


@pytest.mark.asyncio
async def test_jackdaw_runs_reviewer_when_declared() -> None:
    """When both implementer + reviewer are present, the reviewer runs and
    its critique reaches the aggregator."""
    primus_client, primus_calls = _client(["the plan", "final summary"])
    impl_client, impl_calls = _client(["wrote the JSON method"])
    review_client, review_calls = _client(["missing null-check for optional fields"])

    subordinates = {
        "implementer": _subordinate(impl_client, role="implementer"),
        "reviewer": _subordinate(review_client, role="reviewer"),
    }
    jackdaw = JackdawPrimusPersona(primus_client, subordinates=subordinates)

    response = await jackdaw.handle_task(_task())
    assert response.status.state == "completed"
    assert len(impl_calls) == 1
    assert len(review_calls) == 1

    # The reviewer's input includes the implementer output
    reviewer_input = review_calls[0]["messages"][-1]["content"]
    assert "wrote the JSON method" in reviewer_input
    assert "PLAN given to implementer" in reviewer_input

    # The aggregator sees the reviewer's critique
    agg_user = primus_calls[1]["messages"][-1]["content"]
    assert "REVIEWER CRITIQUE" in agg_user
    assert "null-check" in agg_user


@pytest.mark.asyncio
async def test_jackdaw_skips_reviewer_when_not_declared() -> None:
    """No reviewer in subordinates → aggregate prompt has no REVIEWER block."""
    primus_client, primus_calls = _client(["plan", "summary"])
    impl_client, _ = _client(["impl output"])
    subordinates = {"implementer": _subordinate(impl_client)}
    jackdaw = JackdawPrimusPersona(primus_client, subordinates=subordinates)
    await jackdaw.handle_task(_task())
    agg_user = primus_calls[1]["messages"][-1]["content"]
    assert "REVIEWER" not in agg_user
    assert "IMPLEMENTER OUTPUT" in agg_user


@pytest.mark.asyncio
async def test_jackdaw_subordinate_hooks_fire() -> None:
    """SUBORDINATE_DISPATCHED / SUBORDINATE_RETURNED fire for each subordinate."""
    hook_registry = HookRegistry()
    dispatched: list[dict[str, Any]] = []
    returned: list[dict[str, Any]] = []
    hook_registry.register(HookName.SUBORDINATE_DISPATCHED, lambda p: dispatched.append(p))
    hook_registry.register(HookName.SUBORDINATE_RETURNED, lambda p: returned.append(p))

    primus_client, _ = _client(["plan", "summary"])
    sub_client, _ = _client(["impl reply"])
    jackdaw = JackdawPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        hook_registry=hook_registry,
    )

    await jackdaw.handle_task(_task())
    assert len(dispatched) == 1
    assert dispatched[0]["role"] == "implementer"
    assert len(returned) == 1


@pytest.mark.asyncio
async def test_jackdaw_model_override() -> None:
    """Passing model=... overrides the default model."""
    primus_client, primus_calls = _client(["plan", "summary"])
    sub_client, _ = _client(["impl"])
    jackdaw = JackdawPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        model="custom/model-x",
    )
    assert jackdaw.model == "custom/model-x"
    await jackdaw.handle_task(_task())
    assert primus_calls[0]["model"] == "custom/model-x"
