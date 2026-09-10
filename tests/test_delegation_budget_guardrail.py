"""DelegationBudgetGuardrail — cabal#146.

The guard this complements, DoomLoopGuardrail, keys on (target, payload) and
is therefore defeated by rewording. A production incident produced ~270 asks
of materially one question across a dozen wordings, each counting separately
toward a threshold of 3. These tests pin the property that closes that: a
counter that never looks at the words.
"""

from __future__ import annotations

import pytest

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import (
    MAX_DELEGATIONS_PER_CONTEXT,
    DelegationBudgetGuardrail,
)


def _task(
    text: str = "q",
    context_id: str | None = "ctx-1",
    target_agent: str = "target",
    task_id: str = "t-1",
) -> Task:
    kwargs: dict = {
        "id": task_id,
        "status": TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
        "metadata": {"cabal_target_agent": target_agent},
    }
    if context_id is not None:
        kwargs["contextId"] = context_id
    return Task(**kwargs)


def _ctx() -> GuardrailContext:
    return GuardrailContext(persona_name="caller", task_id="t-1", phase="router")


@pytest.mark.asyncio
async def test_rewording_does_not_evade_the_budget() -> None:
    """THE INCIDENT, in miniature.

    Twelve distinct wordings of one question walked past a sameness threshold
    of 3 because each wording started its own count. Here every wording is
    distinct and the budget still stops it.
    """
    g = DelegationBudgetGuardrail(limit=3)
    wordings = [
        "Do you know the Fleet Host Project ID?",
        "What is the Fleet Host Project ID?",
        "What is the Fleet Host Project ID and Number?",
        "Do you know the Fleet Host Project ID/Number?",
    ]
    verdicts = [(await g.check(_task(w), _ctx())).type for w in wordings]
    assert verdicts == [
        VerdictType.PASS,
        VerdictType.PASS,
        VerdictType.PASS,
        VerdictType.BLOCK,
    ]


@pytest.mark.asyncio
async def test_a_different_target_does_not_get_a_fresh_budget() -> None:
    """The incident storm hit two colleagues. A per-target budget would have
    doubled the ceiling; this counts the conversation, not the callee."""
    g = DelegationBudgetGuardrail(limit=2)
    assert (await g.check(_task(target_agent="a"), _ctx())).type is VerdictType.PASS
    assert (await g.check(_task(target_agent="b"), _ctx())).type is VerdictType.PASS
    assert (await g.check(_task(target_agent="c"), _ctx())).type is VerdictType.BLOCK


@pytest.mark.asyncio
async def test_every_delegation_after_the_cap_also_blocks() -> None:
    """This is why BLOCK is sufficient without changing router semantics.

    DoomLoopGuardrail's count can fall back below its threshold, so its BLOCK
    is a nudge. This counter only rises, so once over budget the agent cannot
    reword its way through -- which is the property the incident needed.
    """
    g = DelegationBudgetGuardrail(limit=1)
    assert (await g.check(_task("first"), _ctx())).type is VerdictType.PASS
    for i in range(5):
        v = await g.check(_task(f"attempt {i}"), _ctx())
        assert v.type is VerdictType.BLOCK, f"attempt {i} slipped through"


@pytest.mark.asyncio
async def test_the_block_reason_tells_the_agent_rewording_will_not_help() -> None:
    """The doom-loop message says 'change approach', and a model changes
    approach by rewording -- which is what defeated it. This message must not
    repeat that mistake."""
    g = DelegationBudgetGuardrail(limit=1)
    await g.check(_task(), _ctx())
    v = await g.check(_task(), _ctx())
    assert v.type is VerdictType.BLOCK
    assert v.reason is not None
    assert "rephrasing" in v.reason
    assert "limit 1" in v.reason


@pytest.mark.asyncio
async def test_separate_conversations_keep_separate_budgets() -> None:
    g = DelegationBudgetGuardrail(limit=1)
    assert (await g.check(_task(context_id="ctx-a"), _ctx())).type is VerdictType.PASS
    assert (await g.check(_task(context_id="ctx-b"), _ctx())).type is VerdictType.PASS
    assert (await g.check(_task(context_id="ctx-a"), _ctx())).type is VerdictType.BLOCK


@pytest.mark.asyncio
async def test_without_a_context_id_the_budget_falls_back_to_the_task_id() -> None:
    """Weaker than intended, never absent: an un-threaded caller still gets a
    budget rather than an unbounded one."""
    g = DelegationBudgetGuardrail(limit=1)
    a = await g.check(_task(context_id=None, task_id="t-a"), _ctx())
    b = await g.check(_task(context_id=None, task_id="t-a"), _ctx())
    assert a.type is VerdictType.PASS
    assert b.type is VerdictType.BLOCK


@pytest.mark.asyncio
async def test_a_non_task_target_passes() -> None:
    """input/output-phase checks are not delegations."""
    g = DelegationBudgetGuardrail(limit=1)
    assert (await g.check("some text", _ctx())).type is VerdictType.PASS
    assert (await g.check(["messages"], _ctx())).type is VerdictType.PASS


@pytest.mark.asyncio
async def test_budget_is_charged_at_attempt_time_with_no_refund() -> None:
    """DELIBERATE, and an earlier docstring got it backwards.

    `check()` increments before the hop and nothing rolls it back, so a
    delegation to an unreachable colleague costs the same as a successful one.
    That is required: the incident stormed a colleague scaled to ZERO
    replicas, so if unreachable targets were free the exact case this guard
    defends against would be unbounded.

    This test exists to stop someone "fixing" that into a refund. If you are
    here because you want failed dispatches to be free, read the incident
    first -- an agent that chose to ask again spent an ask, whether or not
    anyone was listening.
    """
    g = DelegationBudgetGuardrail(limit=2)
    for _ in range(2):
        assert (await g.check(_task(), _ctx())).type is VerdictType.PASS
    # Whatever the caller went on to do with those two dispatches -- succeed,
    # time out, hit a dead endpoint -- the third is refused.
    assert (await g.check(_task(), _ctx())).type is VerdictType.BLOCK

@pytest.mark.asyncio
async def test_no_input_shape_is_free() -> None:
    """The teeth behind the no-refund rule.

    An earlier version of this asserted no method named refund/release/
    decrement existed, which the pre-pass correctly called theatre: it catches
    someone ADDING a refund method and misses the likelier regression, which
    is someone editing `check()` to skip the increment under some condition.

    This charges across every axis a future condition might key on -- target,
    payload, task id -- and asserts the count equals the number of calls. Any
    "this kind of delegation is free" branch makes the arithmetic disagree.
    """
    g = DelegationBudgetGuardrail(limit=99)
    shapes = [
        _task("a", target_agent="x"),
        _task("b", target_agent="y"),
        _task("", target_agent="z"),
        _task("a", target_agent="x", task_id="other"),
        _task("a" * 5000, target_agent="x"),
    ]
    for t in shapes:
        assert (await g.check(t, _ctx())).type is VerdictType.PASS
    assert g._counts["ctx-1"] == len(shapes), (
        "some input shape was charged nothing -- if that is deliberate, the "
        "budget is no longer a count of delegations"
    )


def test_the_increment_is_unconditional() -> None:
    """THE ACTUAL TEETH, and it took two attempts to get here.

    A behavioural test can only catch a free-pass condition keyed on an input
    shape it happens to enumerate -- proven by trying: a mutant that skipped
    the increment for an empty canonical payload survived the shape-based test
    above, because none of its shapes produce one.

    So assert the structure instead, the same way test_check_is_await_free
    does. Exactly two early returns may precede the increment -- the non-Task
    guard and the empty-session-key guard, both documented. A third is someone
    adding "this kind of delegation is free", which is how this regresses.
    """
    import inspect

    src = inspect.getsource(DelegationBudgetGuardrail.check)
    before = src.split("count = self._counts.pop")[0]
    assert before.count("return ") == 2, (
        "an early return was added or removed before the counter increments. "
        "Two are expected (non-Task target, missing session key). A third "
        "means some delegation is now free -- see the no-refund rule."
    )


def test_a_policy_may_lower_the_budget_but_never_raise_it() -> None:
    """A policy-only limit fails open when the policy is missing; the ceiling
    lives in code so a misconfigured deployment is still bounded."""
    assert DelegationBudgetGuardrail(limit=5).limit == 5
    assert DelegationBudgetGuardrail(limit=10_000).limit == MAX_DELEGATIONS_PER_CONTEXT
    assert DelegationBudgetGuardrail().limit == MAX_DELEGATIONS_PER_CONTEXT


def test_a_zero_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        DelegationBudgetGuardrail(limit=0)


@pytest.mark.asyncio
async def test_tracked_conversations_are_bounded() -> None:
    """A long-lived process must not leak one counter per conversation."""
    g = DelegationBudgetGuardrail(limit=5)
    g._maxsize = 4
    for i in range(50):
        await g.check(_task(context_id=f"ctx-{i}"), _ctx())
    assert len(g._counts) <= 4


@pytest.mark.asyncio
async def test_check_is_await_free() -> None:
    """A primus fan-out calls one shared instance concurrently; an await-free
    async def runs atomically on a cooperative loop, so the counter cannot
    race. Any await added here reintroduces that race."""
    import inspect

    src = inspect.getsource(DelegationBudgetGuardrail.check)
    assert "await " not in src
