"""The doom-loop TTL must stay above the federated dispatch timeout.

DoomLoopGuardrail counts N repeats of a delegation key *within* its TTL and
resets a key that hasn't recurred inside the window. A hanging federated peer
burns the whole read ceiling per attempt, so once CABAL_DISPATCH_TIMEOUT_S
reaches the TTL every check lands outside the window, the count resets to 1,
and the guard never fires — on the one path (kind="federated") where it is the
sole enforcement point.

Found in review of #123. The first test here is the regression proof: it fails
against the unpatched wiring (a bare 600s default TTL).
"""

from __future__ import annotations

import pytest

from cabal.config.identity import GuardrailsConfig, IdentityConfig
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import (
    DOOM_LOOP_TTL_SAFETY_FACTOR,
    _doom_loop_ttl,
    build_router_input_guardrails,
)
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import DoomLoopGuardrail


def _task() -> Task:
    """A single repeating delegation — the doom loop being guarded against."""
    return Task(
        id="t-1",
        contextId="ctx-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="same payload")]),
        ),
        metadata={"cabal_target_agent": "slow-peer"},
    )


def _ctx() -> GuardrailContext:
    return GuardrailContext(persona_name="caller", task_id="t-1", phase="router")


async def _blocks_when_every_attempt_burns(ttl: float | None, timeout_s: float) -> bool:
    """Drive the guard with a peer that hangs for the full read ceiling.

    The clock advances by one whole timeout between checks — the worst case
    the TTL has to survive — so this measures the real interaction rather than
    asserting the arithmetic back to itself.
    """
    clock = [0.0]
    guard = DoomLoopGuardrail(threshold=5, ttl_seconds=ttl, _now=lambda: clock[0])
    for _ in range(12):  # well past threshold=5
        verdict = await guard.check(_task(), _ctx())
        if verdict.type is VerdictType.BLOCK:
            return True
        clock[0] += timeout_s
    return False


@pytest.mark.asyncio
async def test_guard_still_fires_at_a_timeout_that_exceeds_the_default_ttl() -> None:
    """REGRESSION (#123 review): 900s dispatch vs the bare 600s default TTL."""
    timeout_s = 900.0
    assert await _blocks_when_every_attempt_burns(
        _doom_loop_ttl(600.0, timeout_s), timeout_s
    )


@pytest.mark.asyncio
async def test_the_unpatched_default_ttl_is_genuinely_defeated() -> None:
    """Pins WHY the fix is needed — this is the bug, reproduced."""
    assert not await _blocks_when_every_attempt_burns(600.0, 900.0)


@pytest.mark.asyncio
async def test_short_timeouts_were_never_affected() -> None:
    """The default TTL already covered these; the fix must not disturb them."""
    for timeout_s in (30.0, 120.0, 599.0):
        assert await _blocks_when_every_attempt_burns(
            _doom_loop_ttl(600.0, timeout_s), timeout_s
        )


def test_ttl_is_left_alone_when_already_clear_of_the_timeout() -> None:
    assert _doom_loop_ttl(600.0, 30.0) == 600.0


def test_ttl_is_raised_to_the_safety_factor_when_too_low() -> None:
    assert _doom_loop_ttl(600.0, 900.0) == 900.0 * DOOM_LOOP_TTL_SAFETY_FACTOR


def test_disabled_ttl_passes_through_untouched() -> None:
    """None means no time-based reset, which is already immune."""
    assert _doom_loop_ttl(None, 9000.0) is None


def _doom_loop_guard(guardrails: list) -> DoomLoopGuardrail:
    """Pick the doom-loop guard out of the chain BY TYPE, not by position.

    These two tests originally destructured a single-element list, because the
    chain held exactly one guard when this PR was written. Main has since added
    DelegationBudgetGuardrail (#146) and DelegationEdgeGuardrail (#154) to the
    same builder, so `[guard] = ...` raises "too many values to unpack" -- a
    merge consequence, not a defect in either change. Selecting by type is also
    the assertion these tests actually wanted: they are about the TTL reaching
    the doom-loop guard, not about how many guards exist beside it.
    """
    found = [g for g in guardrails if isinstance(g, DoomLoopGuardrail)]
    assert len(found) == 1, f"expected exactly one DoomLoopGuardrail, got {len(found)}"
    return found[0]


def test_wiring_passes_the_resolved_ttl_to_the_guard() -> None:
    """The resolver is only useful if build_router_input_guardrails uses it."""
    identity = IdentityConfig(guardrails=GuardrailsConfig(doom_loop_ttl_s=600.0))
    guard = _doom_loop_guard(
        build_router_input_guardrails(identity, dispatch_timeout_s=900.0)
    )
    assert guard._ttl == 900.0 * DOOM_LOOP_TTL_SAFETY_FACTOR


def test_wiring_default_matches_the_historical_timeout() -> None:
    guard = _doom_loop_guard(build_router_input_guardrails(None))
    assert guard._ttl == 600.0
