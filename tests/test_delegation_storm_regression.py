"""The production delegation storm, end to end, against the real guard chain.

THE INCIDENT (cabal#146). Two agents in a production cluster exchanged one
cluster-metadata question roughly 270 times. Each ask was REPHRASED, so each
carried a different doom-loop key and the sameness counter — threshold 5 —
never tripped. The task never terminated; the agent was eventually
liveness-killed, restarted, resumed from persisted state, and REBUILT THE
STORM against a counter that had not persisted.

WHY THIS FILE EXISTS SEPARATELY from `test_delegation_budget_persistence.py`.
That file pins the budget guard's mechanism in isolation. This one asserts the
INCIDENT is bounded: it builds the real router's real guard chain from the real
default config via `build_router_input_guardrails`, drives it with the four
wordings quoted in cabal#146, and then throws the process away and rebuilds it
against the same database — which is the half the incident actually turned on.

A mechanism test and an incident test fail for different reasons, and the
incident is the thing we promised an operator we had bounded.

THE CONTROL IS LOAD-BEARING. `test_the_rephrasing_defeats_the_sameness_counter`
is not colour: it asserts that the storm gets past the doom-loop guard, which
is what made cabal#146 a defect rather than a configuration mistake. Without
it, the budget assertions would pass just as well on a build where the doom
loop stopped everything at 5 and the budget was never reached — i.e. on a
build that had not fixed anything.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import create_app
from cabal.core.guardrails import GuardrailContext, VerdictType, run_guardrails
from cabal.core.guardrails_builtin import MAX_DELEGATIONS_PER_CONTEXT

CONVERSATION = "the-storm-conversation"

#: Verbatim from cabal#146. Four wordings of ONE question, which is what the
#: agent actually produced when the doom-loop BLOCK told it to change approach:
#: a language model changes approach by rewording, and rewording moves it off
#: the key the counter was watching.
WORDINGS = [
    "Do you know the Fleet Host Project ID, Fleet Host Project Number, Fleet Membership Name",
    "What is the Fleet Host Project ID, Membership name, and Connect Gateway registration repository",
    "What is the Fleet Host Project, Membership name, and Connect Gateway registration repository",
    "Do you know the Fleet Host Project ID/Number and Connect Gateway GitHub repository",
]


def _ask(n: int, context_id: str = CONVERSATION) -> Task:
    """One rephrased ask, same conversation, same colleague."""
    return Task(
        **{
            "id": f"task-{uuid.uuid4().hex[:8]}",
            "contextId": context_id,
            "status": TaskStatus(
                state="submitted",
                message=Message(
                    role="user",
                    parts=[TextPart(text=f"{WORDINGS[n % 4]} (attempt {n}, rephrased)")],
                ),
            ),
            "metadata": {"cabal_target_agent": "colleague-under-storm"},
        }
    )


def _ctx() -> GuardrailContext:
    return GuardrailContext(persona_name="storming-agent", task_id="t", phase="router")


async def _storm(app, *, limit: int = 400, context_id: str = CONVERSATION):
    """Send rephrased asks until the chain refuses. Returns (n_passed, verdict).

    Drives the ROUTER'S OWN chain — `app.state.dispatch_router._input_guardrails`
    — rather than guards this test constructed, so a regression in how they are
    wired fails here too, not only a regression in the guards themselves.
    """
    guards = app.state.dispatch_router._input_guardrails
    ctx = _ctx()
    passed = 0
    for n in range(limit):
        verdict = await run_guardrails(guards, _ask(n, context_id), ctx)
        if verdict.type is VerdictType.PASS:
            passed += 1
            continue
        return passed, verdict
    return passed, None


@pytest_asyncio.fixture
async def boot(tmp_path: Path):
    """Boot the daemon, and boot it AGAIN against the same database.

    The `app` fixture cannot serve this test: it yields one app, and the
    property under test is what a SECOND process sees. The database path is
    pinned here rather than taken per-boot, because "same database, new
    process" is the whole experiment.
    """
    db_file = tmp_path / "data" / "cabal.db"
    db_file.parent.mkdir(exist_ok=True)
    os.environ["CABAL_DB_URL"] = f"sqlite:///{db_file}"
    url = f"sqlite+aiosqlite:///{db_file}"
    started: list = []

    async def _boot():
        app = create_app(agent="all", config_path="identity.toml", db_url=url)
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        started.append(cm)
        return app

    yield _boot
    for cm in reversed(started):
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_the_rephrasing_defeats_the_sameness_counter(boot) -> None:
    """THE CONTROL, and the reason cabal#146 was a defect.

    The doom-loop threshold is 5. If rephrasing did not defeat it, the storm
    would stop at 5 and every assertion below would pass on a build that fixed
    nothing. So this pins that the storm gets WELL past the sameness counter,
    which is what forced a payload-independent bound to exist at all.
    """
    app = await boot()
    from cabal.config.identity import GuardrailsConfig

    threshold = GuardrailsConfig().doom_loop_threshold
    passed, verdict = await _storm(app)

    assert passed > threshold * 2, (
        f"only {passed} rephrased asks got through against a doom-loop "
        f"threshold of {threshold} — the sameness counter is stopping this "
        "storm, so the budget assertions below would prove nothing"
    )
    assert "doom" not in verdict.reason.lower(), (
        f"the storm was stopped by the sameness counter, not the budget: {verdict.reason}"
    )


@pytest.mark.asyncio
async def test_the_storm_terminates_and_stays_terminated(boot) -> None:
    """It ends, and it ends TERMINALLY rather than advisorily (cabal#152).

    In the incident the guard advised, the agent took the advice ~270 times,
    and an agent with no terminal state has exactly one move when stuck.
    """
    app = await boot()
    passed, verdict = await _storm(app)

    assert passed == MAX_DELEGATIONS_PER_CONTEXT
    assert verdict.type is VerdictType.TERMINATE, (
        f"the storm was refused with {verdict.type.name}, not TERMINATE — an "
        "advisory refusal is what the agent rephrased past 270 times"
    )
    # The agent keeps trying, as it did. It must keep being refused.
    again = await run_guardrails(
        app.state.dispatch_router._input_guardrails, _ask(9_999), _ctx()
    )
    assert again.type is VerdictType.TERMINATE


@pytest.mark.asyncio
async def test_a_restart_does_not_rebuild_the_storm(boot) -> None:
    """THE HALF THE INCIDENT TURNED ON (cabal#174).

    "The task never terminated; the agent was eventually liveness-killed,
    restarted, resumed from persisted state, and rebuilt the storm." Task state
    persisted; the counter did not. Against `c7c8058` — the commit before the
    fix — this test sees a full fresh budget after the restart; it is the
    control that makes the assertion below mean something, and it was run.
    """
    app = await boot()
    spent, verdict = await _storm(app)
    assert spent == MAX_DELEGATIONS_PER_CONTEXT
    assert verdict.type is VerdictType.TERMINATE

    # Let the write-behind land before the process "dies".
    guard = next(
        g
        for g in app.state.dispatch_router._input_guardrails
        if hasattr(g, "_writes")
    )
    while guard._writes:
        import asyncio

        await asyncio.gather(*list(guard._writes), return_exceptions=True)

    # THE RESTART. A new process, the same database, the same conversation.
    app2 = await boot()
    passed_after, verdict_after = await _storm(app2)

    assert passed_after == 0, (
        f"the restart granted {passed_after} fresh delegations to a conversation "
        "that had already spent its budget — this is cabal#146's storm being "
        "rebuilt, which is how the incident actually continued"
    )
    assert verdict_after is not None
    assert verdict_after.type is VerdictType.TERMINATE


@pytest.mark.asyncio
async def test_an_unrelated_conversation_is_not_punished_by_the_storm(boot) -> None:
    """The bound is per conversation, so a storm must not deny service to
    everyone else. Without this, 'nothing gets through after a storm' would
    satisfy every assertion above."""
    app = await boot()
    await _storm(app)

    verdict = await run_guardrails(
        app.state.dispatch_router._input_guardrails,
        _ask(0, context_id="an-innocent-conversation"),
        _ctx(),
    )
    assert verdict.type is VerdictType.PASS, (
        "a storm in one conversation refused an unrelated one — the budget is "
        "supposed to be keyed on the conversation, not global"
    )
