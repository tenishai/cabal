"""A truncated LLM response must not be recorded as a successful task (cabal#134).

`finish_reason` appeared zero times in the source before this. A model that
stopped at its output ceiling returned an empty answer, and cabal marked the
task `completed` and fired `dispatch_success` — measured on three live
dispatches, the worst of which burned 65,536 tokens over 15½ minutes and
reported a win.
"""

from __future__ import annotations

import pytest

from cabal.agents.personas.wright import WrightPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.codes import (
    CODE_BUDGET_EXHAUSTED,
    CODE_ESCALATION,
    CODE_POLICY_VIOLATION,
)
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.abort import TaskAborted
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient, LLMTruncatedError


def _task(tid: str = "t-1") -> Task:
    return Task(
        id=tid,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="do the thing")]),
        ),
    )


def _client(*responses: dict) -> LiteLLMClient:
    """A client returning the given raw responses in order, last one repeating."""
    seq = list(responses)

    async def fn(**kwargs):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())


def _persona() -> Subordinate:
    """A persona for the classifier tests, which call check_truncation with a
    separately built response and never drive the client. The first version
    stubbed a client here whose response was never used, which read as though
    the stub drove the check."""
    return Subordinate("wright", "implementer", _client(_resp("unused")), model="m")


def _resp(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


# --- the classifier's three cases ------------------------------------------


def test_ceiling_with_no_content_raises() -> None:
    """THE DEFECT. This is what produced t-502753ab9f95 and its two siblings."""
    p = _persona()
    with pytest.raises(LLMTruncatedError, match="output ceiling"):
        p.check_truncation(_resp("", "length"), "")


def test_ceiling_with_content_is_kept_but_flagged() -> None:
    """A truncated-but-useful answer is still worth returning — with a flag."""
    p = _persona()
    assert p.check_truncation(_resp("half an answer", "length"), "half an answer") is True


def test_a_tool_calls_reason_without_any_calls_is_a_failure() -> None:
    """The blessing keys on the MESSAGE carrying calls, not on the reason
    string. A response claiming `tool_calls` with none attached is one cabal
    could never execute — `_extract_tool_calls` reads `message.tool_calls` —
    so it would previously have completed as an empty answer. Failing it is
    the same improvement as failing the legacy `function_call` shape."""
    p = _persona()
    with pytest.raises(LLMTruncatedError, match="tool_calls"):
        p.check_truncation(_resp(None, "tool_calls"), "")


def test_whitespace_only_counts_as_no_content() -> None:
    """A model cut off after a newline has not answered."""
    p = _persona()
    with pytest.raises(LLMTruncatedError):
        p.check_truncation(_resp("\n  \n", "length"), "\n  \n")


def test_a_normal_answer_is_untouched() -> None:
    p = _persona()
    assert p.check_truncation(_resp("hi", "stop"), "hi") is False


# --- end to end through a real dispatch ------------------------------------


@pytest.mark.asyncio
async def test_a_truncated_persona_dispatch_fails_the_task() -> None:
    """Before: state='completed' with an empty answer."""
    p = Subordinate("wright", "implementer", _client(_resp("", "length")), model="m")
    out = await p.handle_task(_task())

    assert out.status.state == "failed", out.status.state
    assert out.metadata["cabal_reason_code"] == CODE_BUDGET_EXHAUSTED
    assert "output ceiling" in out.metadata["cabal_reason_message"]


@pytest.mark.asyncio
async def test_a_truncated_primus_plan_fails_before_fanning_out() -> None:
    """An empty plan hands the implementer an empty instruction.

    Only the PLAN is truncated here; the later hops return normally. A stub
    that truncated every call would pass even with the plan check removed,
    because the aggregate hop would fail instead — the mutation caught exactly
    that, so the stage has to be isolated for the test to mean anything.
    """
    client = _client(_resp("", "length"), _resp("work"), _resp("summary"))
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-plan"))

    assert out.status.state == "failed"
    assert out.metadata["cabal_reason_code"] == CODE_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_a_truncated_primus_aggregate_fails_the_task() -> None:
    """Every empty answer in #134's evidence came from this hop: the plan and
    the subordinate succeed, and the aggregate is cut off."""
    client = _client(
        _resp("a fine plan"),          # plan
        _resp("implementer output"),   # subordinate
        _resp("", "length"),           # aggregate, truncated
    )
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-agg"))

    assert out.status.state == "failed", out.status.state
    assert out.metadata["cabal_reason_code"] == CODE_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_an_untruncated_fan_out_still_completes() -> None:
    """Guards the guard: the fix must not fail healthy dispatches."""
    client = _client(_resp("plan"), _resp("work"), _resp("summary"))
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-ok"))

    assert out.status.state == "completed", out.status.state


# --- review findings on PR #135 --------------------------------------------


def test_content_filter_with_no_content_is_a_failure() -> None:
    """REVIEW, Important 3. The first rule asked `if finish_reason != "length"`,
    which defaulted every unenumerated reason to fine — so `content_filter`
    with empty content reproduced cabal#134 exactly, one string comparison
    from the fix."""
    p = _persona()
    with pytest.raises(LLMTruncatedError, match="content_filter"):
        p.check_truncation(_resp("", "content_filter"), "")


def test_an_unknown_future_reason_with_no_content_is_a_failure() -> None:
    """The point of permitting a shape rather than denying a reason: a reason
    nobody has thought of yet must not default to success."""
    p = _persona()
    with pytest.raises(LLMTruncatedError, match="not a reason cabal treats"):
        p.check_truncation(_resp("", "some_new_provider_reason"), "")


def test_a_missing_reason_with_no_content_is_a_failure() -> None:
    p = _persona()
    with pytest.raises(LLMTruncatedError):
        p.check_truncation({"choices": [{"message": {"content": ""}}]}, "")


def test_tool_calls_are_blessed_whatever_the_reason_says() -> None:
    """An EXECUTABLE tool call is a legitimate empty even when the reason is
    missing or odd — otherwise tightening the rule would break the tool loop.

    The call must be executable: this originally used `[{"id": "1"}]`, which
    `_extract_tool_calls` skips for want of a `function` key, so it asserted
    the looser predicate that round 4 replaced.
    """
    p = _persona()
    resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "1", "function": {"name": "f", "arguments": "{}"}}
                    ],
                }
            }
        ]
    }
    assert p.check_truncation(resp, "") is False


def test_an_empty_stop_completes_but_is_flagged() -> None:
    """ROUND 3, Important 1. `stop` does not mean "the model chose to say
    nothing": litellm collapses ERROR, MALFORMED_RESPONSE, network_error and
    FINISH_REASON_UNSPECIFIED onto it, and defaults every unmapped reason there
    too — exactly the states that arrive empty.

    The task still completes, because an empty answer reaches a human who can
    see it is empty. But it is flagged, because otherwise the WORSE outcome
    (nothing at all) carried LESS machine-readable signal than the better one
    (half an answer), which is inverted severity."""
    p = _persona()
    assert p.check_truncation(_resp("", "stop"), "") is True


@pytest.mark.asyncio
async def test_a_failed_leg_fails_the_fan_out() -> None:
    """REVIEW, Important 1 — the path this PR is named for.

    _dispatch_subordinate read the leg's message parts without looking at its
    state, so a failed leg's REASON STRING became its output and the aggregator
    wrote a confident summary on top. The task completed clean — worse than the
    empty answer it replaced, because an operator can spot zero characters.
    """
    client = _client(
        _resp("a fine plan"),   # plan
        _resp("", "length"),    # the leg is truncated -> annotated failed
        _resp("summary"),       # aggregate would happily summarise the reason
    )
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-leg"))

    assert out.status.state == "failed", out.status.state
    assert out.metadata["cabal_reason_code"] == CODE_BUDGET_EXHAUSTED
    assert "implementer" in out.metadata["cabal_reason_message"]


@pytest.mark.asyncio
async def test_a_truncated_plan_with_content_also_fails() -> None:
    """REVIEW, Important 2. A cut-off ANSWER reaches a human who can see it
    stops mid-word; a cut-off PLAN reaches an agent that cannot tell a
    truncated instruction from a terse one."""
    client = _client(_resp("Step 1: refactor the wid", "length"), _resp("x"), _resp("y"))
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-halfplan"))

    assert out.status.state == "failed"
    assert "truncated instruction" in out.metadata["cabal_reason_message"]


@pytest.mark.asyncio
async def test_a_truncated_aggregate_completes_but_says_so() -> None:
    """The flag was computed and dropped on the floor. The summary is still the
    answer, so the task completes — it just admits it was cut off."""
    client = _client(_resp("plan"), _resp("work"), _resp("half a summ", "length"))
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-halfagg"))

    assert out.status.state == "completed"
    assert out.metadata["cabal_truncated"] == "aggregate"


@pytest.mark.asyncio
async def test_an_untruncated_fan_out_is_not_flagged() -> None:
    """Guards the guard: the metadata key must not appear on healthy tasks."""
    client = _client(_resp("plan"), _resp("work"), _resp("summary"))
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-clean"))

    assert out.status.state == "completed"
    assert "cabal_truncated" not in out.metadata


# --- round-2 review findings ------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_plan_fails_even_when_the_reason_is_stop() -> None:
    """ROUND 2, Important 1 — the escape was `stop`.

    check_truncation blesses empty-with-`stop`, so `_plan` returned "" and the
    fan-out proceeded: the implementer received the empty string as its entire
    instruction and the aggregator wrote a summary over nothing. cabal#134's
    exact harm, reached through a different reason.

    Not hypothetical: litellm's `map_finish_reason` defaults EVERY reason it
    cannot map to `stop` (verified against litellm 1.92.0), including provider
    error states — the ones most likely to arrive with empty content.
    """
    seen: list[str] = []

    class _Recording(Subordinate):
        async def handle_task(self, task, **kw):
            seen.append(task.status.message.parts[0].text)
            return await super().handle_task(task, **kw)

    client = _client(_resp("", "stop"), _resp("work"), _resp("summary"))
    implementer = _Recording("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-emptyplan"))

    assert out.status.state == "failed", out.status.state
    assert "no instruction to hand a subordinate" in out.metadata["cabal_reason_message"]
    assert seen == [], f"the fan-out ran on an empty instruction: {seen!r}"


@pytest.mark.asyncio
async def test_a_persona_stamps_the_truncation_flag_too() -> None:
    """ROUND 2, nit 4. A truncated-but-kept LEG output completed with nothing
    machine-readable, so a primus summarising it had no signal to see."""
    p = _persona()
    p.llm_client = _client(_resp("half an ans", "length"))

    out = await p.handle_task(_task("t-flagged"))

    assert out.status.state == "completed"
    assert out.metadata["cabal_truncated"] == "wright-implementer"


@pytest.mark.asyncio
async def test_a_clean_persona_dispatch_is_not_flagged() -> None:
    p = _persona()
    p.llm_client = _client(_resp("a whole answer"))

    out = await p.handle_task(_task("t-unflagged"))

    assert "cabal_truncated" not in out.metadata


@pytest.mark.asyncio
async def test_an_unexplained_leg_failure_is_704_not_701() -> None:
    """ROUND 2, nit 6. 701 asserts a safety-tier refusal the code cannot know
    happened — the confident-wrong-label class this PR exists to remove."""
    client = _client(_resp("plan"), _resp("work"), _resp("summary"))
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    # a leg that fails with no reason code at all
    async def _failed(task, **kw):
        return task.model_copy(
            update={"status": TaskStatus(state="failed", message=None), "metadata": {}}
        )

    implementer.handle_task = _failed  # type: ignore[method-assign]
    out = await wright.handle_task(_task("t-unexplained"))

    assert out.status.state == "failed"
    assert out.metadata["cabal_reason_code"] == CODE_ESCALATION


@pytest.mark.asyncio
async def test_an_abort_outranks_a_leg_failure() -> None:
    """ROUND 2, nit 3. Results are in insertion order, so raising the first
    BaseException let leg 1's failure win over leg 2's cancellation — an
    operator abort reported as a budget failure."""
    from cabal.agents.primus import _LegFailedError

    client = _client(_resp("plan"), _resp("summary"))
    a = Subordinate("wright", "implementer", client, model="m")
    b = Subordinate("wright", "second", client, model="m")
    wright = WrightPrimusPersona(
        client, subordinates={"implementer": a, "second": b}, model="m"
    )

    async def _fails(task, **kw):
        raise _LegFailedError(role="implementer", code=CODE_BUDGET_EXHAUSTED, message="ceiling")

    async def _aborts(task, **kw):
        raise TaskAborted("t-abortrace.second", "operator cancelled")

    async def _dispatch(parent_task, *, role, instruction):
        return await (_fails if role == "implementer" else _aborts)(parent_task)

    wright._dispatch_subordinate = _dispatch  # type: ignore[method-assign]

    with pytest.raises(TaskAborted):
        await wright.handle_task(_task("t-abortrace"))


@pytest.mark.asyncio
async def test_an_empty_aggregate_completes_but_says_so() -> None:
    """ROUND 3, Important 1, end to end on the hop that produced every row of
    cabal#134's evidence. A provider ERROR normalises to `stop` and arrives
    empty; the task completes, and now carries the flag it was missing."""
    client = _client(_resp("plan"), _resp("work"), _resp("", "stop"))
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    out = await wright.handle_task(_task("t-emptyagg"))

    assert out.status.state == "completed"
    assert out.metadata["cabal_truncated"] == "aggregate"


@pytest.mark.asyncio
async def test_an_empty_persona_answer_completes_but_says_so() -> None:
    """The same inverted severity on a singleton's terminal answer."""
    p = _persona()
    p.llm_client = _client(_resp("", "stop"))

    out = await p.handle_task(_task("t-emptyans"))

    assert out.status.state == "completed"
    assert out.metadata["cabal_truncated"] == "wright-implementer"


def test_litellm_really_does_collapse_error_states_onto_stop() -> None:
    """The premise the whole asymmetry argument turns on, pinned against the
    installed litellm rather than quoted from a review.

    If a future litellm stops collapsing these, the reasoning in
    check_truncation's docstring needs revisiting — this fails and says so.
    """
    # SKIPS IN CI, AND THAT IS THE HONEST OUTCOME. litellm is an UNDECLARED
    # dependency (cabal#88), so a clean install does not have it and this test
    # cannot check anything there — it failed CI on exactly that, while passing
    # locally where an older install still provides it. Asserting against a
    # package that may be absent has to skip rather than fail; when #88 lands
    # and litellm is declared, this starts running in CI with no change here.
    litellm_helpers = pytest.importorskip(
        "litellm.litellm_core_utils.core_helpers",
        reason="litellm is an undeclared dependency (cabal#88)",
    )
    map_finish_reason = litellm_helpers.map_finish_reason

    for provider_reason in (
        "ERROR",
        "MALFORMED_RESPONSE",
        "MALFORMED_FUNCTION_CALL",
        "network_error",
        "FINISH_REASON_UNSPECIFIED",
    ):
        assert map_finish_reason(provider_reason) == "stop", provider_reason
    # and anything unmapped defaults there too
    assert map_finish_reason("SOMETHING_INVENTED_NEXT_QUARTER") == "stop"


@pytest.mark.asyncio
async def test_a_doom_loop_outranks_a_leg_failure() -> None:
    """ROUND 3, nit 1. The precedence between the two DEFERRED kinds was
    asserted by comment only — swapping the raises left the suite green.

    Doom wins: a guardrail block is a structural refusal (the leg never ran),
    while a leg failure is an outcome of one that did.
    """
    from cabal.agents.primus import _DoomLoopError, _LegFailedError

    client = _client(_resp("plan"), _resp("summary"))
    a = Subordinate("wright", "implementer", client, model="m")
    b = Subordinate("wright", "second", client, model="m")
    wright = WrightPrimusPersona(
        client, subordinates={"implementer": a, "second": b}, model="m"
    )

    async def _dispatch(parent_task, *, role, instruction):
        if role == "implementer":
            raise _LegFailedError(
                role="implementer", code=CODE_BUDGET_EXHAUSTED, message="ceiling"
            )
        raise _DoomLoopError("repeated delegation")

    wright._dispatch_subordinate = _dispatch  # type: ignore[method-assign]
    out = await wright.handle_task(_task("t-doomfirst"))

    assert out.status.state == "failed"
    assert out.metadata["cabal_reason_code"] == CODE_POLICY_VIOLATION, (
        "a guardrail block must outrank a leg failure"
    )


def test_an_unexecutable_tool_call_array_is_not_blessed() -> None:
    """ROUND 4, nit 1 — the mirror of the case closed in round 3.

    `_extract_tool_calls` skips any entry without a `function` key, so an array
    of such entries yields nothing cabal can execute. Blessing it on "the array
    is non-empty" made the two predicates disagree in the silent direction.
    """
    from cabal.agents.llm_persona import _extract_tool_calls

    resp = {
        "choices": [
            {
                "message": {"content": None, "tool_calls": [{"id": "call_1"}]},
                "finish_reason": "tool_calls",
            }
        ]
    }
    assert _extract_tool_calls(resp) == [], "nothing executable in this response"
    with pytest.raises(LLMTruncatedError):
        _persona().check_truncation(resp, "")


def test_a_real_tool_call_is_still_blessed() -> None:
    """Guards the guard: tightening the predicate must not break the tool loop."""
    resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "f", "arguments": "{}"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    assert _persona().check_truncation(resp, "") is False
