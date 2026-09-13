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
default config via `build_router_input_guardrails`, drives it with SIXTEEN
wordings of one question — the count is load-bearing and the reason is under
WORDINGS — and then throws the process away and rebuilds it against the same
database — which is the half the incident actually turned on.

A mechanism test and an incident test fail for different reasons, and the
incident is the thing we promised an operator we had bounded.

WHAT THIS FILE COVERS, AGAINST cabal#146'S OWN ACCOUNTING, because a
regression test that overstates its coverage is worse than a smaller honest
one — and an earlier version of this docstring got the accounting wrong. It
listed {rephrasing, TTL, the restart} as "cabal#146's three evasions". The
restart is not one of them: it comes from the incident's narrative and is
cabal#174's subject. Substituting it silently dropped the first evasion the
issue actually names. Caught in pre-pass.

The issue names these three, under "Why the existing guard did not stop it":

  1. IT ADVISES RATHER THAN HALTS — BLOCK is a nudge, so the agent took the
     advice ~270 times. EXERCISED here, by
     `test_the_storm_terminates_and_stays_terminated`, which pins TERMINATE
     rather than BLOCK. Closed by cabal#152, not by the budget.
  2. THE COUNTER KEYS ON THE PAYLOAD, so paraphrase resets it. EXERCISED, and
     the wording count below is sized so the budget is demonstrably the guard
     that fires rather than the sameness counter.
  3. THE TTL (600s by default), where a loop pausing longer than the window
     "restarts from count 1 (intentional)" — so a SLOW storm evades the counter
     even with byte-identical payloads. NOT EXERCISED. These tests run in
     milliseconds and no key ever ages out.

And separately from the three: THE RESTART, which is how the incident actually
continued and what cabal#174 closed. Exercised, and the only claim in this file
with a witness in the repo's history.
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

#: Wordings of ONE question. The first four are verbatim from cabal#146 — what
#: the agent actually produced when the doom-loop BLOCK told it to change
#: approach, since a language model changes approach by rewording and rewording
#: moves it off the key the counter watches.
#:
#: THE COUNT IS LOAD-BEARING AND WAS MEASURED, not chosen for realism. The
#: doom-loop guard fires at `(threshold - 1) * distinct_wordings` asks; the
#: budget fires at its limit. So with the shipped defaults (threshold 5, limit
#: 40) the DOOM LOOP is what stops a storm of ten wordings or fewer, and the
#: BUDGET only becomes the operative guard above ten:
#:
#:      wordings :  1   2   4   8   9  10  12  20
#:      asks     :  4   8  16  32  36  40  40  40
#:      stopped  : doom …………………………………… doom | budget ……
#:
#: An earlier version of this file used four wordings and appended
#: "(attempt N)" to each, which made every payload unique BY CONSTRUCTION — so
#: the doom counter could never see a repeat and the harness proved nothing
#: about rephrasing at all. Caught in pre-pass. Sixteen real wordings, no
#: suffix, is what puts the budget on the hook.
WORDINGS = [
    "Do you know the Fleet Host Project ID, Fleet Host Project Number, Fleet Membership Name",
    "What is the Fleet Host Project ID, Membership name, and Connect Gateway registration repository",
    "What is the Fleet Host Project, Membership name, and Connect Gateway registration repository",
    "Do you know the Fleet Host Project ID/Number and Connect Gateway GitHub repository",
    "Could you tell me the Fleet Host Project identifier and the Membership name",
    "I need the Fleet Host Project Number plus the Connect Gateway repo",
    "Which project hosts the fleet, and what is the membership called",
    "Can you confirm the Fleet Host Project and its Connect Gateway registration",
    "Remind me of the fleet host project id and the gateway repository please",
    "What are the fleet host project number and membership name again",
    "Do you have the Connect Gateway registration repo and the host project",
    "Tell me the membership name and the fleet host project identifier",
    "Sorry — the Fleet Host Project ID and the Connect Gateway repository",
    "Just the fleet host project and membership name, if you have them",
    "Looking for the Fleet Host Project Number and the gateway registration repo",
    "One more time: fleet host project, membership name, Connect Gateway repository",
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
                    # NO UNIQUE SUFFIX. Appending one would make every payload
                    # distinct by construction and the doom counter unable to
                    # see a repeat, which is a harness artefact rather than the
                    # incident's mechanism.
                    parts=[TextPart(text=WORDINGS[n % len(WORDINGS)])],
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
async def test_the_budget_is_what_stops_this_storm_not_the_sameness_counter(
    boot,
) -> None:
    """THE CONTROL — and an earlier version of it claimed more than it showed.

    It said that without this test "every budget assertion below would pass
    just as well on a build where the doom loop stopped everything at 5". Two
    things were wrong with that, both caught in pre-pass:

    1. The two tests below assert `== MAX_DELEGATIONS_PER_CONTEXT`, so on such
       a build they would fail on the count (5 != 40), not pass. Only
       `..._an_unrelated_conversation_is_not_punished` is genuinely vacuous
       without this one.
    2. The harness appended "(attempt N)" to every payload, making each unique
       BY CONSTRUCTION. The doom counter could never see a repeat, so the
       control was proving "a unique payload defeats a same-payload counter" —
       true by construction, and nothing to do with the incident.

    What this now asserts is the measured relationship. The doom guard fires at
    `(threshold - 1) * distinct_wordings`; the budget fires at its limit. With
    the shipped defaults that crossover sits just above TEN wordings, so a
    storm of a dozen is the first shape the sameness counter cannot hold — and
    is why a payload-independent bound had to exist at all (cabal#146).
    """
    from cabal.config.identity import GuardrailsConfig

    cfg = GuardrailsConfig()
    crossover = cfg.delegation_budget_limit / max(cfg.doom_loop_threshold - 1, 1)
    assert len(WORDINGS) > crossover, (
        f"{len(WORDINGS)} wordings against a doom threshold of "
        f"{cfg.doom_loop_threshold} and a budget of {cfg.delegation_budget_limit}: "
        f"the doom loop would fire first (at {(cfg.doom_loop_threshold - 1) * len(WORDINGS)} "
        "asks), so every budget assertion in this file would be measuring the "
        "wrong guard"
    )

    app = await boot()
    passed, verdict = await _storm(app)

    assert passed == cfg.delegation_budget_limit
    assert verdict.type is VerdictType.TERMINATE, (
        f"stopped with {verdict.type.name} after {passed} asks — the doom loop "
        "returns BLOCK and the conversation budget returns TERMINATE, so this "
        "storm was held by the sameness counter and the budget is untested"
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
    #
    # TOLERANT OF A GUARD WITH NO `_writes`, AND THAT IS THE WITNESS (#179 r1
    # Important 1). `_writes` arrived with the cabal#174/#175 persistence work,
    # so at `c7c8058` -- the tree this test is supposed to fail against -- the
    # strict `next(...)` raised StopIteration, which Python converts to
    # `RuntimeError: coroutine raised StopIteration` inside the coroutine. The
    # test died HERE, before `app2` was ever booted. Red on that tree, and red
    # for the wrong reason: a missing harness attribute rather than an
    # unpersisted counter. A future reader re-running the witness -- the entire
    # point of documenting one -- could not tell a regressed guard from a
    # broken harness.
    #
    # SAME FAILURE as the defect the first pre-pass caught in this file: the
    # HARNESS, not the mechanism, deciding the result -- there by manufacturing
    # a pass, here by dying before the property. Twice in one file, which is
    # why this comment is long.
    #
    # They are not the same SPECIES, and an earlier version of this comment
    # said "by construction" of both (#179 r2). The suffix defect baked its
    # conclusion into the setup and was vacuous forever, on every commit. This
    # one reached no outcome at all, and only ever affected the archaeological
    # re-run of the witness on a historical tree -- the shipped test was
    # correct throughout, since a strict `next` cannot fire where the guard
    # has `_writes`.
    import asyncio

    guard = next(
        (
            g
            for g in app.state.dispatch_router._input_guardrails
            if hasattr(g, "_writes")
        ),
        None,
    )
    while guard is not None and guard._writes:
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
