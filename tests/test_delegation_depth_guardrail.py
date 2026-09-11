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
    for junk in ("nonsense", None, [], {"a": 1}, -5, float("nan")):
        t = _task()
        t.metadata[DEPTH_KEY] = junk
        assert delegation_depth(t) == 0, junk
        assert next_delegation_depth(t) == 1, junk


@pytest.mark.asyncio
async def test_an_infinite_depth_is_garbage_too_rather_than_an_exception() -> None:
    """cabal#144 pre-pass, WRONG 1 — THE SHAPE THE LIST ABOVE DID NOT HAVE.

    `int(float("inf"))` raises `OverflowError`, which is neither `TypeError`
    nor `ValueError`, so the only filter this function had let it straight
    through — turning "a garbage value costs one hop of slack rather than
    crashing the dispatch" into its opposite for the one value most obviously
    chosen on purpose. `nan` lands on `ValueError` and always worked, which is
    why the hole was invisible: the neighbouring float shape was fine.

    GOES THROUGH `model_validate` RATHER THAN ASSIGNING METADATA, because the
    finding is about reachability as much as arithmetic. Pydantic keeps the
    value unchanged, and Python's own `json` module parses a bare `Infinity`
    token into this float, so it arrives over the wire and not only from a
    test.
    """
    import json

    for token, value in (("Infinity", float("inf")), ("-Infinity", float("-inf"))):
        assert json.loads(f'{{"d": {token}}}')["d"] == value, token
        t = Task.model_validate(
            {
                "id": "t-inf",
                "status": {"state": "submitted"},
                "metadata": {DEPTH_KEY: value},
            }
        )
        assert t.metadata[DEPTH_KEY] == value, "pydantic changed the value"
        assert delegation_depth(t) == 0, token
        assert next_delegation_depth(t) == 1, token

    guard = DelegationDepthGuardrail(max_depth=3)
    t = _task()
    t.metadata[DEPTH_KEY] = float("inf")
    assert (await guard.check(t, _ctx())).type is VerdictType.PASS, (
        "an infinite claimed depth must read as the top of the chain, not crash"
    )


def test_a_policy_cannot_raise_the_ceiling() -> None:
    """CLAMPED, NOT HONOURED. A policy-only limit fails open exactly when the
    policy is missing, which is the state a misconfigured or newly-added
    persona is in.

    NOT the same shape as the budget's limit, which an earlier version of this
    docstring claimed (#144 pre-pass): the budget RAISES below 1 where this
    clamps up to it. The shape this DOES match is the one #156 gives the
    primus's `max_legs` — which is not in this tree yet, so read that as the
    intended convergence rather than as a precedent you can grep (#144 r1).
    See the config-validator test below for where the axes were made to agree.
    """
    assert DelegationDepthGuardrail(max_depth=10_000).max_depth == MAX_DELEGATION_DEPTH
    assert DelegationDepthGuardrail(max_depth=0).max_depth == 1
    assert DelegationDepthGuardrail(max_depth=3).max_depth == 3


def test_a_depth_limit_below_one_is_refused_at_config_load() -> None:
    """The axis that had no config validator (#144 pre-pass, WRONG 6).

    `delegation_depth_limit = 0` in identity.toml loaded silently and the guard
    floored it to 1; the same typo in `delegation_budget_limit` refuses to
    start. The guard's clamp stays — a depth of 1 is a working bound, unlike a
    budget of 0 — so this is about the operator hearing about a value they
    cannot have meant, not about what the guard does with it.
    """
    from pydantic import ValidationError

    from cabal.config.identity import GuardrailsConfig

    assert GuardrailsConfig().delegation_depth_limit == 8
    for bad in (0, -1):
        with pytest.raises(ValidationError, match="delegation_depth_limit must be >= 1"):
            GuardrailsConfig(delegation_depth_limit=bad)


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
