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
from cabal.core.a2a.codes import CODE_BUDGET_EXHAUSTED
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
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


def _resp(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


# --- the classifier's three cases ------------------------------------------


def test_ceiling_with_no_content_raises() -> None:
    """THE DEFECT. This is what produced t-502753ab9f95 and its two siblings."""
    p = Subordinate("wright", "implementer", _client(_resp("")), model="m")
    with pytest.raises(LLMTruncatedError, match="output ceiling"):
        p.check_not_truncated(_resp("", "length"), "")


def test_ceiling_with_content_is_kept_but_flagged() -> None:
    """A truncated-but-useful answer is still worth returning — with a flag."""
    p = Subordinate("wright", "implementer", _client(_resp("x")), model="m")
    assert p.check_not_truncated(_resp("half an answer", "length"), "half an answer") is True


def test_tool_calls_with_no_content_stays_legitimate() -> None:
    """parse_response's docstring calls this 'a legitimate (if unusual) final
    state'. The fix must not turn it into a failure."""
    p = Subordinate("wright", "implementer", _client(_resp("")), model="m")
    assert p.check_not_truncated(_resp(None, "tool_calls"), "") is False


def test_whitespace_only_counts_as_no_content() -> None:
    """A model cut off after a newline has not answered."""
    p = Subordinate("wright", "implementer", _client(_resp("")), model="m")
    with pytest.raises(LLMTruncatedError):
        p.check_not_truncated(_resp("\n  \n", "length"), "\n  \n")


def test_a_normal_answer_is_untouched() -> None:
    p = Subordinate("wright", "implementer", _client(_resp("hi")), model="m")
    assert p.check_not_truncated(_resp("hi", "stop"), "hi") is False


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
