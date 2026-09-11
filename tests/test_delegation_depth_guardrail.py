"""cabal#144 — a delegation chain is bounded topologically, not only by spend."""

from __future__ import annotations

import pytest

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import (
    DEPTH_KEY,
    MAX_DELEGATION_DEPTH,
    DelegationDepthGuardrail,
    delegation_depth,
    next_delegation_depth,
)


def _task(depth: int | None = None) -> Task:
    t = Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="q")]),
        ),
    )
    if depth is not None:
        t.metadata[DEPTH_KEY] = depth
    return t


def _ctx() -> GuardrailContext:
    return GuardrailContext(persona_name="caller", task_id="t-1", phase="router")


@pytest.mark.asyncio
async def test_a_chain_is_refused_once_it_reaches_the_limit() -> None:
    g = DelegationDepthGuardrail(max_depth=3)
    assert (await g.check(_task(2), _ctx())).type is VerdictType.PASS
    v = await g.check(_task(3), _ctx())
    assert v.type is VerdictType.BLOCK
    assert "depth 3" in v.reason and "limit of 3" in v.reason


@pytest.mark.asyncio
async def test_a_task_a_human_started_is_at_depth_zero() -> None:
    """No metadata means nobody delegated to get here."""
    assert delegation_depth(_task()) == 0
    assert (await DelegationDepthGuardrail(max_depth=1).check(_task(), _ctx())).type is (
        VerdictType.PASS
    )


@pytest.mark.asyncio
async def test_an_unreadable_depth_is_treated_as_the_top_of_the_chain() -> None:
    """DELIBERATE, and the direction matters. A malformed value must not be a
    way to CLAIM a lower depth than you have — but it is not an error either.
    Treating it as 0 means anything delegating onward is stamped 1 and bounded
    normally from there, so a garbage value costs one hop of slack rather than
    either crashing the dispatch or disabling the bound."""
    for junk in ("nonsense", None, [], {"a": 1}, -5):
        t = _task()
        t.metadata[DEPTH_KEY] = junk
        assert delegation_depth(t) == 0, junk
        assert next_delegation_depth(t) == 1, junk


def test_a_policy_cannot_raise_the_ceiling() -> None:
    """CLAMPED, NOT HONOURED — the same shape as the budget's limit and the
    primus's max_legs, so all three axes are bounded the same way. A
    policy-only limit fails open exactly when the policy is missing, which is
    the state a misconfigured or newly-added persona is in."""
    assert DelegationDepthGuardrail(max_depth=10_000).max_depth == MAX_DELEGATION_DEPTH
    assert DelegationDepthGuardrail(max_depth=0).max_depth == 1
    assert DelegationDepthGuardrail(max_depth=3).max_depth == 3


@pytest.mark.asyncio
async def test_a_non_task_target_passes() -> None:
    assert (await DelegationDepthGuardrail().check("text", _ctx())).type is (
        VerdictType.PASS
    )


def test_check_is_await_free() -> None:
    """Same invariant its two siblings carry: a primus fan-out calls guardrails
    concurrently across legs sharing one instance."""
    import inspect

    assert "await " not in inspect.getsource(DelegationDepthGuardrail.check)
