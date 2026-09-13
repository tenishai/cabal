"""Mason primus + Subordinate dispatch tests."""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.personas.mason import MASON_SYSTEM_PROMPT, MasonPrimusPersona
from cabal.agents.primus import MAX_LEGS_PER_FANOUT
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.codes import CODE_POLICY_VIOLATION, reason_code
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.guardrails_builtin import (
    DEPTH_KEY,
    DelegationBudgetGuardrail,
    DelegationDepthGuardrail,
    DoomLoopGuardrail,
    next_delegation_depth,
)
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
        id="t-mason-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _subordinate(client: LiteLLMClient, role: str = "implementer") -> Subordinate:
    return Subordinate(
        primus="mason", role=role, llm_client=client, model="anthropic/claude-sonnet-4-6"
    )


@pytest.mark.asyncio
async def test_mason_dispatches_to_implementer_and_aggregates() -> None:
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
    mason = MasonPrimusPersona(primus_client, subordinates=subordinates)

    response = await mason.handle_task(_task())
    assert response.status.state == "completed"
    assert "implementer wrote a parser" in response.status.message.parts[0].text

    # Two primus calls: plan + aggregate. agent name + trace get stripped
    # from kwargs before reaching the stub (they're hook-payload only), so
    # distinguish by which system prompt was sent.
    assert len(primus_calls) == 2
    assert "You are Cabal's Mason" in primus_calls[0]["messages"][0]["content"]
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
    mason = MasonPrimusPersona(
        primus_client, subordinates=subordinates, hook_registry=hook_registry
    )

    await mason.handle_task(_task())

    assert len(dispatched) == 1
    assert dispatched[0]["role"] == "implementer"
    assert dispatched[0]["parent_task_id"] == "t-mason-1"

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
        primus="mason", role="implementer", llm_client=sub_client,
        model="anthropic/claude-sonnet-4-6",
    )

    mason = MasonPrimusPersona(primus_client, subordinates={"implementer": captured})
    task = _task()
    task.metadata["cabal_trace_id"] = "a" * 32
    task.metadata["cabal_span_id"] = "b" * 16
    await mason.handle_task(task)

    sub_meta = captured_sub_task["metadata"]
    assert sub_meta["cabal_trace_id"] == "a" * 32
    assert sub_meta["cabal_span_id"] != "b" * 16  # fresh span per hop
    assert sub_meta["cabal_parent_task"] == "t-mason-1"
    assert sub_meta["cabal_subordinate_role"] == "implementer"


@pytest.mark.asyncio
async def test_mason_requires_implementer() -> None:
    primus_client, _ = _client([])
    with pytest.raises(ValueError, match="non-reviewer"):
        MasonPrimusPersona(primus_client, subordinates={})


# ---- reviewer subordinate (Phase P) ----------------------------------------


@pytest.mark.asyncio
async def test_mason_runs_reviewer_when_declared() -> None:
    """When both implementer + reviewer are present, the reviewer runs and
    its critique reaches the aggregator."""
    primus_client, primus_calls = _client(["the plan", "final summary"])
    impl_client, impl_calls = _client(["wrote a JSON parser"])
    review_client, review_calls = _client(
        ["the parser doesn't handle unicode escapes"]
    )
    subordinates = {
        "implementer": _subordinate(impl_client, role="implementer"),
        "reviewer": _subordinate(review_client, role="reviewer"),
    }
    mason = MasonPrimusPersona(primus_client, subordinates=subordinates)

    response = await mason.handle_task(_task())
    assert response.status.state == "completed"
    # Both subordinates ran exactly once
    assert len(impl_calls) == 1
    assert len(review_calls) == 1
    # The reviewer's input message includes the implementer output
    reviewer_input = review_calls[0]["messages"][-1]["content"]
    assert "wrote a JSON parser" in reviewer_input
    assert "PLAN given to the leg" in reviewer_input
    # The aggregator sees the reviewer's critique
    agg_user = primus_calls[1]["messages"][-1]["content"]
    assert "REVIEWER CRITIQUE" in agg_user
    assert "unicode escapes" in agg_user


@pytest.mark.asyncio
async def test_mason_skips_reviewer_block_when_not_declared() -> None:
    """No reviewer in subordinates → aggregate prompt has no REVIEWER block,
    so existing (Phase K) operators don't see prompt drift."""
    primus_client, primus_calls = _client(["plan", "summary"])
    impl_client, _ = _client(["impl output"])
    subordinates = {"implementer": _subordinate(impl_client)}
    mason = MasonPrimusPersona(primus_client, subordinates=subordinates)
    await mason.handle_task(_task())
    agg_user = primus_calls[1]["messages"][-1]["content"]
    assert "REVIEWER" not in agg_user
    assert "[implementer] OUTPUT" in agg_user


@pytest.mark.asyncio
async def test_reviewer_dispatch_fires_subordinate_hooks() -> None:
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
    impl_client, _ = _client(["impl"])
    review_client, _ = _client(["no significant issues"])
    subordinates = {
        "implementer": _subordinate(impl_client, role="implementer"),
        "reviewer": _subordinate(review_client, role="reviewer"),
    }
    mason = MasonPrimusPersona(
        primus_client, subordinates=subordinates, hook_registry=hook_registry
    )
    await mason.handle_task(_task())

    # Expect TWO of each — once for implementer, once for reviewer
    assert [d["role"] for d in dispatched] == ["implementer", "reviewer"]
    assert [r["role"] for r in returned] == ["implementer", "reviewer"]


@pytest.mark.asyncio
async def test_reviewer_runs_after_implementer_strictly() -> None:
    """The reviewer prompt must contain the implementer's output, which
    means dispatch order is implementer → reviewer, not parallel."""
    primus_client, _ = _client(["plan", "summary"])
    impl_calls: list[str] = []
    review_calls: list[str] = []

    async def impl_fn(model: str, messages: list, **kwargs):
        impl_calls.append("impl")
        return {"choices": [{"message": {"content": "specific impl output"}}]}

    async def review_fn(model: str, messages: list, **kwargs):
        # When this runs, impl_calls must already have an entry
        review_calls.append(("review", len(impl_calls)))
        return {"choices": [{"message": {"content": "review verdict"}}]}

    impl_client = LiteLLMClient(acompletion_fn=impl_fn, hook_registry=HookRegistry())
    review_client = LiteLLMClient(acompletion_fn=review_fn, hook_registry=HookRegistry())
    subordinates = {
        "implementer": _subordinate(impl_client, role="implementer"),
        "reviewer": _subordinate(review_client, role="reviewer"),
    }
    mason = MasonPrimusPersona(primus_client, subordinates=subordinates)
    await mason.handle_task(_task())

    # The reviewer ran AFTER the implementer completed
    assert review_calls == [("review", 1)]


@pytest.mark.asyncio
async def test_plan_text_passed_to_aggregate_prompt() -> None:
    """The aggregate call's prompt should contain both the plan and the
    implementer's output, so the LLM can summarize them together."""
    primus_client, primus_calls = _client(
        ["the plan is to do X carefully", "summary"]
    )
    sub_client, _ = _client(["implementer output: did the X thing"])
    subordinates = {"implementer": _subordinate(sub_client)}
    mason = MasonPrimusPersona(primus_client, subordinates=subordinates)
    await mason.handle_task(_task())

    aggregate_messages = primus_calls[1]["messages"]
    final_user = aggregate_messages[-1]["content"]
    assert "the plan is to do X carefully" in final_user
    assert "did the X thing" in final_user


# ---- cabal#59: doom-loop + context_id threading tests ----------------------


def _task_with_ctx(text: str = "do the thing", context_id: str = "ctx-1") -> Task:
    """Build a Task with a stable context_id, mirroring a real chat session."""
    return Task(
        **{
            "id": f"t-ctx-{text[:8]}",
            "contextId": context_id,
            "status": TaskStatus(
                state="submitted",
                message=Message(role="user", parts=[TextPart(text=text)]),
            ),
        }
    )


@pytest.mark.asyncio
async def test_mason_doom_loop_blocks_repeated_identical_subordinate_dispatch() -> None:
    """THE key test for cabal#59: the autonomous loop shape is now caught.

    With a stable context_id and identical plan text (simulating an agent
    stuck in a loop), the Nth subordinate dispatch is blocked at the
    subordinate boundary — returning a policy-violation Task to the caller.

    This tests the shape documented as NOT caught in
    test_doom_loop_guardrail.py::test_distinct_task_ids_without_context_id_never_block:
    fresh task ids, same payload. Now that _dispatch_subordinate threads
    context_id and runs the doom-loop check before calling handle_task,
    the loop IS caught when the parent supplies a stable context_id.
    """
    doom_loop = DoomLoopGuardrail(threshold=2)

    # Both handle_task calls produce the same plan text (same stub response)
    # so the delegation key is identical → counter accumulates.
    primus_client, _ = _client(
        ["identical plan text", "summary", "identical plan text"]
    )
    sub_client, sub_calls = _client(["impl output"])

    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=doom_loop,
    )

    # First dispatch: context_id stable, doom-loop count=1 → PASS.
    task1 = Task(
        **{
            "id": "t-doom-1",
            "contextId": "loop-session",
            "status": TaskStatus(
                state="submitted",
                message=Message(role="user", parts=[TextPart(text="implement X")]),
            ),
        }
    )
    r1 = await mason.handle_task(task1)
    assert r1.status.state == "completed"
    assert len(sub_calls) == 1  # implementer was called once

    # Second dispatch: same context_id + same plan → count=2 >= threshold=2 → BLOCK.
    task2 = Task(
        **{
            "id": "t-doom-2",
            "contextId": "loop-session",
            "status": TaskStatus(
                state="submitted",
                message=Message(role="user", parts=[TextPart(text="implement X")]),
            ),
        }
    )
    r2 = await mason.handle_task(task2)
    assert r2.status.state == "failed"
    assert reason_code(r2) == CODE_POLICY_VIOLATION
    reason_msg = r2.metadata.get("cabal_reason_message") or ""
    assert "mason guardrail blocked" in reason_msg
    assert "identical delegations" in reason_msg
    # Implementer was NOT called a second time — the block happened before dispatch.
    assert len(sub_calls) == 1


@pytest.mark.asyncio
async def test_mason_doom_loop_blocks_in_two_role_pipeline() -> None:
    """cabal#59 (Prudence): the PRODUCTION-DEFAULT two-role pipeline.

    With both implementer and reviewer, each handle_task fans out
    impl, rev — so a loop is the interleaved sequence impl, rev, impl, rev, …
    A consecutive-only counter would reset on every dispatch and NEVER block;
    per-key cumulative counting drives impl(planX) to the threshold across
    handle_task calls and blocks the 3rd. This is the shape the earlier
    implementer-only test did NOT cover.
    """
    doom_loop = DoomLoopGuardrail(threshold=3)
    # Plans are identical across the three runs (a stuck loop); aggregates vary.
    primus_client, _ = _client(
        ["planX", "agg-1", "planX", "agg-2", "planX"]
    )
    impl_client, impl_calls = _client(["impl output", "impl output"])
    review_client, review_calls = _client(["critique", "critique"])

    mason = MasonPrimusPersona(
        primus_client,
        subordinates={
            "implementer": _subordinate(impl_client, role="implementer"),
            "reviewer": _subordinate(review_client, role="reviewer"),
        },
        doom_loop=doom_loop,
    )

    def _task(i: int) -> Task:
        return Task(
            **{
                "id": f"t-2role-{i}",
                "contextId": "loop-session-2role",
                "status": TaskStatus(
                    state="submitted",
                    message=Message(role="user", parts=[TextPart(text="do X")]),
                ),
            }
        )

    # Runs 1 and 2 complete (impl count 1, 2 — interleaved with reviewer).
    assert (await mason.handle_task(_task(1))).status.state == "completed"
    assert (await mason.handle_task(_task(2))).status.state == "completed"
    # Run 3: impl(planX) reaches count=3 → BLOCK before the implementer dispatch.
    r3 = await mason.handle_task(_task(3))
    assert r3.status.state == "failed"
    assert reason_code(r3) == CODE_POLICY_VIOLATION
    assert "mason guardrail blocked" in (r3.metadata.get("cabal_reason_message") or "")
    # Implementer ran on runs 1+2 only; reviewer never reached on run 3.
    assert len(impl_calls) == 2
    assert len(review_calls) == 2


@pytest.mark.asyncio
async def test_mason_doom_loop_different_instructions_do_not_block() -> None:
    """Different plan texts reset the doom-loop counter — no false block.

    Even with threshold=2 and a shared context_id, genuinely different
    delegation instructions never trip the guard.
    """
    doom_loop = DoomLoopGuardrail(threshold=2)

    primus_client, _ = _client(["plan A", "summary A", "plan B", "summary B"])
    sub_client, sub_calls = _client(["impl A", "impl B"])

    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=doom_loop,
    )

    for i in range(2):
        task = Task(
            **{
                "id": f"t-diff-{i}",
                "contextId": "shared-session",
                "status": TaskStatus(
                    state="submitted",
                    message=Message(role="user", parts=[TextPart(text=f"task {i}")]),
                ),
            }
        )
        r = await mason.handle_task(task)
        assert r.status.state == "completed", f"dispatch {i} unexpectedly blocked"

    assert len(sub_calls) == 2


@pytest.mark.asyncio
async def test_context_id_threads_to_sub_task() -> None:
    """Parent task's context_id is inherited by the sub-task (cabal#59).

    This is the mechanism that lets DoomLoopGuardrail group re-delegations:
    without context_id threading every sub-task has a fresh id (session key)
    and the counter never accumulates.
    """
    captured: dict[str, Any] = {}

    class CaptureSub(Subordinate):
        async def handle_task(self, task: Task) -> Task:
            captured["context_id"] = task.context_id
            return await super().handle_task(task)

    primus_client, _ = _client(["plan", "summary"])
    sub_client, _ = _client(["impl reply"])
    cap = CaptureSub(
        primus="mason", role="implementer", llm_client=sub_client,
        model="anthropic/claude-sonnet-4-6",
    )

    # doom_loop=None so we don't accidentally trip the guard in a single call.
    mason = MasonPrimusPersona(
        primus_client, subordinates={"implementer": cap}, doom_loop=None
    )

    parent_task = Task(
        **{
            "id": "t-inherit-1",
            "contextId": "inherited-ctx",
            "status": TaskStatus(
                state="submitted",
                message=Message(role="user", parts=[TextPart(text="work")]),
            ),
        }
    )
    await mason.handle_task(parent_task)

    assert captured["context_id"] == "inherited-ctx"


@pytest.mark.asyncio
async def test_doom_loop_disabled_by_none() -> None:
    """Passing doom_loop=None disables the guard entirely.

    Useful for tests or integrations that intentionally dispatch the same
    instruction multiple times and must not be blocked.
    """
    primus_client, _ = _client(
        ["same plan", "summary", "same plan", "summary", "same plan", "summary"]
    )
    sub_client, sub_calls = _client(["out", "out", "out"])

    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
    )

    context_id = "no-guard-session"
    for i in range(3):
        task = Task(
            **{
                "id": f"t-nodoom-{i}",
                "contextId": context_id,
                "status": TaskStatus(
                    state="submitted",
                    message=Message(role="user", parts=[TextPart(text="do it")]),
                ),
            }
        )
        r = await mason.handle_task(task)
        assert r.status.state == "completed", f"call {i} unexpectedly blocked"

    assert len(sub_calls) == 3


@pytest.mark.asyncio
async def test_primus_fans_out_non_reviewer_legs_concurrently() -> None:
    """cabal#69: non-reviewer subordinates are dispatched CONCURRENTLY.

    Two legs each wait on a shared Barrier(2) before producing output. If the
    legs ran sequentially the first would block forever (only one party ever
    arrives) and time out; reaching the barrier proves they run concurrently.
    Both legs' outputs are then joined into the synthesis (aggregate) prompt.
    """
    import asyncio

    barrier = asyncio.Barrier(2)

    class BarrierSub(Subordinate):
        async def handle_task(self, task: Task) -> Task:
            # Sequential dispatch would deadlock here (a lone party at a 2-party
            # barrier); wait_for turns that into a fast, clear failure.
            await asyncio.wait_for(barrier.wait(), timeout=2.0)
            return await super().handle_task(task)

    primus_client, primus_calls = _client(["plan", "final synthesis"])
    leg_a_client, _ = _client(["legA result"])
    leg_b_client, _ = _client(["legB result"])
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={
            "implementer": BarrierSub(
                primus="mason", role="implementer", llm_client=leg_a_client,
                model="anthropic/claude-sonnet-4-6",
            ),
            "analyzer": BarrierSub(
                primus="mason", role="analyzer", llm_client=leg_b_client,
                model="anthropic/claude-sonnet-4-6",
            ),
        },
    )

    result = await mason.handle_task(_task())
    assert result.status.state == "completed"

    # Both legs' outputs reached the synthesis prompt, each labelled by role.
    agg_user = primus_calls[1]["messages"][-1]["content"]
    assert "[implementer] OUTPUT" in agg_user and "legA result" in agg_user
    assert "[analyzer] OUTPUT" in agg_user and "legB result" in agg_user


def test_mason_system_prompt_frames_sre_and_reliability() -> None:
    """The name Mason encodes "builds the load-bearing layer" — but the model
    only ever sees the system prompt. The module docstring and identity.toml
    both said SRE; the prompt said a generic "engineering primus", so the one
    place the role had to be legible was the one place it wasn't
    (Prudence, PR #113). Mirrors test_wright_primus.py's equivalent.
    """
    low = MASON_SYSTEM_PROMPT.lower()
    assert "sre" in low or "reliability" in low
    assert "infrastructure" in low or "platform" in low
    assert "delegate" in low


@pytest.mark.asyncio
async def test_the_fan_out_is_bounded_by_the_delegation_budget() -> None:
    """cabal#158 — the primus path never reaches the router's guardrail chain.

    `_dispatch_subordinate` calls `handle_task` directly, so a delegation
    budget wired only into `build_router_input_guardrails` bounds
    router-mediated delegation and nothing else. That left fan-out legs
    bounded by the sameness counter alone -- the counter #146 exists precisely
    because rewording defeats it.

    DISTINCT plan text per dispatch, so the doom-loop guard cannot be what
    stops this: if it passed with the budget removed, the test would be
    measuring the wrong guard.
    """
    budget = DelegationBudgetGuardrail(limit=2)
    primus_client, _ = _client(["plan one", "sum", "plan two", "sum", "plan three", "sum"])
    sub_client, _ = _client(["impl"] * 3)
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
        delegation_budget=budget,
    )

    seen = []
    for n in range(3):
        task = Task(
            **{
                "id": f"t-budget-{n}",
                "contextId": "one-conversation",
                "status": TaskStatus(
                    state="submitted",
                    message=Message(role="user", parts=[TextPart(text=f"do {n}")]),
                ),
            }
        )
        result = await mason.handle_task(task)
        seen.append(" ".join(
            p.text for p in result.status.message.parts if isinstance(p, TextPart)
        ) if result.status.message else "")

    assert any("budget" in t.lower() for t in seen), (
        f"three fan-outs under a budget of 2 were not bounded: {seen}"
    )


@pytest.mark.asyncio
async def test_the_primus_shares_the_budget_instance_it_is_given() -> None:
    """THE PROPERTY THAT MAKES THIS WORTH DOING, and the one a second instance
    would quietly break.

    The budget is per-conversation, keyed on context_id. Two instances would
    give one conversation two separate allowances -- an agent mixing router
    hops with fan-out legs would get double the budget while LOOKING bounded,
    which is worse than the gap it replaces.
    """
    budget = DelegationBudgetGuardrail(limit=50)
    primus_client, _ = _client(["a plan", "a summary"])
    sub_client, _ = _client(["impl"])
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
        delegation_budget=budget,
    )
    task = Task(
        **{
            "id": "t-share",
            "contextId": "shared-conversation",
            "status": TaskStatus(
                state="submitted",
                message=Message(role="user", parts=[TextPart(text="go")]),
            ),
        }
    )
    await mason.handle_task(task)
    assert budget._counts, (
        "the fan-out leg never touched the caller's counter, so the primus is "
        "not using the instance it was handed"
    )


@pytest.mark.asyncio
async def test_the_second_guard_at_the_boundary_still_reaches_its_own_limit() -> None:
    """cabal#158 pre-pass r2 — WHY THE GUARD ORDER IS IMMATERIAL, structurally.

    The comment at this boundary used to justify its order by saying the doom
    threshold trips long before the budget limit. That is a fact about the
    shipped defaults AT THE TIME, not about anything the code enforces: the two
    limits have independent validators and no cross-field check.

    So this runs the configuration the old justification forbids —
    threshold=30, budget=2, with doom checked FIRST — and pins the property that
    actually holds: neither guard starves the other before the outcome is
    decided, so the second-listed one still reaches its own trip point. The
    refusal must name the budget, which has not yet blocked anything, rather
    than the doom guard, which never trips at all here.

    IDENTICAL plan text every dispatch, deliberately: rephrased legs miss the
    sameness counter by construction, so distinct text would make the doom guard
    unable to compete and the test would prove nothing about order.
    """
    doom = DoomLoopGuardrail(threshold=30)
    budget = DelegationBudgetGuardrail(limit=2)
    primus_client, _ = _client(["same plan", "sum"] * 3)
    sub_client, _ = _client(["impl"] * 3)
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=doom,
        delegation_budget=budget,
    )

    seen = []
    for n in range(3):
        task = Task(
            **{
                "id": f"t-order-{n}",
                "contextId": "one-conversation",
                "status": TaskStatus(
                    state="submitted",
                    message=Message(role="user", parts=[TextPart(text="do the thing")]),
                ),
            }
        )
        result = await mason.handle_task(task)
        seen.append(
            " ".join(
                p.text for p in result.status.message.parts if isinstance(p, TextPart)
            )
            if result.status.message
            else ""
        )

    refusals = [t for t in seen if "budget" in t.lower()]
    assert refusals, (
        f"the budget was listed second and never reached its limit of 2: {seen}"
    )
    # THE DOOM GUARD WAS CONSULTED AND SIMPLY DID NOT TRIP, which is the half
    # of "neither starves the other" that the line above cannot show. Reading
    # its counter rather than its message: an earlier version of this assertion
    # looked for "loop" in the refusal text, and DoomLoopGuardrail's message
    # says "identical delegations ... pausing; change approach" -- no such
    # substring, so it could not have failed whatever the guard did.
    #
    # NON-EMPTY, NOT A COUNT (#158 r2 Important 1). The replacement for that
    # inert assertion read `max(counted) == 3`, which is only ever true of the
    # FIRST-listed guard -- so it pinned the guard order inside the test whose
    # thesis is that the order is immaterial, and swapping the tuple turned the
    # suite red with a message that read as a defect report for a non-defect.
    # "Consulted at all" is the order-independent form of the same property,
    # and it keeps its teeth: under a loop that checks only the first guard,
    # either this is empty or `refusals` above is.
    counted = [c for per_key in doom._state.values() for c, _ in per_key.values()]
    assert counted, (
        "the doom guard was never consulted, so the budget's refusal says "
        "nothing about whether the two share the boundary"
    )
    assert not any("identical delegations" in t for t in seen), (
        f"a doom threshold of 30 refused inside three dispatches: {seen}"
    )


async def test_the_fan_out_stamps_and_accumulates_depth() -> None:
    """cabal#144 — depth is a property of the CHAIN, not of one primus's view.

    Inherited-plus-one rather than recomputed, so a depth that ARRIVES from
    outside keeps counting instead of resetting to 1.

    THE ARITHMETIC IS WHAT THIS PINS, and only that. An earlier docstring here
    went on to say the alternative was "a cycle through a remote peer resets to
    1 on every return and the bound never fires" — describing a round trip this
    tree cannot perform (#144 r2). The value stamped below never leaves the
    process: its task goes straight to `subord.handle_task`, the router never
    sees it, and a federated hop forwards metadata without incrementing. So
    inheritance only ever matters for a depth an EXTERNAL client produced, which
    is what daedalus#40 exists to send.

    Recomputing would still be wrong, and that is worth a test — it is the
    difference between believing an arriving chain and overwriting it.
    """
    from cabal.core.guardrails_builtin import DEPTH_KEY

    seen: list[int] = []

    class _Recording:
        async def check(self, target, ctx):
            from cabal.core.guardrails import Verdict

            seen.append(target.metadata.get(DEPTH_KEY))
            return Verdict.pass_()

    mason = MasonPrimusPersona(
        _client(["a plan", "sum"])[0],
        subordinates={"implementer": _subordinate(_client(["impl"])[0])},
        doom_loop=_Recording(),
    )
    # A task already three hops deep: its legs must be stamped four, not one.
    task = _task_with_ctx("go")
    task.metadata[DEPTH_KEY] = 3
    await mason.handle_task(task)
    assert seen == [4], f"expected the leg stamped at parent+1, got {seen}"


def test_a_fan_out_wider_than_the_ceiling_is_refused_at_construction() -> None:
    """cabal#156 — breadth is its own axis, and it is a CONFIG error.

    #144 bounds how DEEP a chain runs; #146 bounds how MANY delegations a
    conversation makes in total. Neither bounds how many children one agent
    opens AT ONCE — forty concurrent legs sit inside a depth bound of 8 and
    inside a total budget of 40, by spending the whole budget in one burst.
    Concurrency costs connections and tokens simultaneously rather than over
    time, which is the liveness starvation that killed the container.

    REFUSED AT CONSTRUCTION, and r1 is why. `leg_roles` is derived from
    `subordinates`, which is assigned once and never mutated, so the comparison
    is invariant for the life of the instance — it cannot depend on the task.
    Checking it only at dispatch meant a deployment declaring nine legs
    constructed fine and then failed EVERY dispatch forever, each one after
    paying for a full plan call. It now fails at boot, for free.
    """
    subs = {f"impl{n}": _subordinate(_client(["out"])[0]) for n in range(5)}
    with pytest.raises(ValueError, match="above the ceiling of 2"):
        MasonPrimusPersona(
            _client(["a plan"])[0], subordinates=subs, doom_loop=None, max_legs=2
        )




@pytest.mark.asyncio
async def test_the_dispatch_check_remains_as_a_backstop() -> None:
    """The runtime check is unreachable through __init__ by design, so this
    reaches it the only way anything could: by mutating `subordinates` after
    construction — which is exactly the future change the backstop guards.

    Kept rather than deleted because "never mutated" is a property of today's
    code, not a guarantee, and the constructor check is the one that would go
    stale if it ever stopped being true.
    """
    leg_calls = []
    subs = {}
    for n in range(2):
        client, calls = _client(["out"])
        leg_calls.append(calls)
        subs[f"impl{n}"] = _subordinate(client)
    mason = MasonPrimusPersona(
        _client(["a plan"])[0], subordinates=subs, doom_loop=None, max_legs=2
    )
    for n in range(2, 5):
        client, calls = _client(["out"])
        leg_calls.append(calls)
        mason.subordinates[f"impl{n}"] = _subordinate(client)

    result = await mason.handle_task(_task_with_ctx("build"))
    text = " ".join(
        p.text for p in result.status.message.parts if isinstance(p, TextPart)
    ) if result.status.message else ""

    assert "configured with 5" in text
    assert "ceiling of 2" in text
    assert "no plan can avoid it" in text
    # REFUSED BEFORE THE GATHER, not after (#156 r1 Important 2). The refusal
    # text is byte-identical whether or not legs were dispatched first, so
    # moving the check below _dispatch_legs left the whole suite green. The
    # legs are what the assertion has to look at.
    assert all(calls == [] for calls in leg_calls), (
        f"legs were dispatched before the breadth check refused: "
        f"{[len(c) for c in leg_calls]}"
    )




@pytest.mark.asyncio
async def test_a_policy_cannot_raise_the_breadth_ceiling() -> None:
    """CLAMPED, NOT HONOURED, mirroring #146's limit at the CEILING. A
    policy-only bound fails open exactly when the policy is missing, and a
    deployment must not be able to raise its own ceiling."""
    subs = {"impl": _subordinate(_client(["out"])[0])}
    mason = MasonPrimusPersona(
        _client(["plan"])[0], subordinates=subs, doom_loop=None, max_legs=10_000
    )
    assert mason._max_legs == MAX_LEGS_PER_FANOUT




@pytest.mark.parametrize("bad", [0, -5])
def test_a_sub_one_breadth_is_clamped_up_rather_than_refused(bad: int) -> None:
    """WHERE THIS STOPS MIRRORING #146's LIMIT, pinned because nothing did.

    The #156 pre-pass found the PR claiming the two clamps mirror each other
    *exactly*. At the ceiling they do -- both silently clamp down. At the floor
    they diverge: `DelegationBudgetGuardrail(limit=0)` raises a documented
    `ValueError`, while `max_legs=0` is quietly substituted with 1.

    The divergence is left standing rather than fixed, and this test is the
    reason it can be: substituting 1 is FAIL-CLOSED. A budget of 0 would be a
    bound nothing can satisfy, so refusing to boot is the honest answer there;
    a fan-out of 1 is a legal, working, maximally-narrow fan-out. Making it
    raise would turn a config typo into a daemon that will not start, for no
    safety gained.

    What was actually wrong was the word "exactly", and that is now the PR's
    problem rather than the code's. If this behaviour is ever changed to raise,
    this test is where the argument above has to be answered.
    """
    subs = {"impl": _subordinate(_client(["out"])[0])}
    mason = MasonPrimusPersona(
        _client(["plan"])[0], subordinates=subs, doom_loop=None, max_legs=bad
    )
    assert mason._max_legs == 1

    with pytest.raises(ValueError, match="limit must be >= 1"):
        DelegationBudgetGuardrail(limit=bad)




@pytest.mark.asyncio
async def test_the_plan_hop_is_told_the_budget() -> None:
    """cabal#156 r1 Important 1 — THE AGENT THAT SPENDS IT IS THE PRIMUS.

    The note was folded into `LLMPersona.build_messages` alone, on the belief
    that that is where every persona's system content is composed. A primus
    overrides `handle_task` and never calls it: instrumented across a real
    Mason dispatch, the note landed on the LEG and on neither the plan nor the
    aggregate call. A `Subordinate` is a leaf that cannot delegate, so the
    number reached the one agent that cannot spend it and was withheld from
    the one deciding how wide to fan out.

    A TEST ON A BARE `LLMPersona` CANNOT SEE THIS, which is why the first one
    did not: that is the single shape where `build_messages` is on the path.
    The guard's precondition selected the case that passes.

    Drives the real guard and a real primus, and asserts on the PLAN call
    specifically rather than on "some call saw it".
    """
    from cabal.core.guardrails_builtin import DelegationBudgetGuardrail

    primus_client, primus_calls = _client(["a plan", "a summary"])
    sub_client, _ = _client(["impl"])
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
    )
    from cabal.core.guardrails import GuardrailContext

    task = _task_with_ctx("build")
    await DelegationBudgetGuardrail(limit=20).check(
        task, GuardrailContext(persona_name="caller", task_id=task.id, phase="router")
    )

    await mason.handle_task(task)

    plan_system = primus_calls[0]["messages"][0]["content"]
    assert "1 of 20 used" in plan_system, (
        "the hop that decides how wide to fan out was not told its budget"
    )
    aggregate_system = primus_calls[-1]["messages"][0]["content"]
    assert "of 20 used" not in aggregate_system, (
        "the aggregate hop spends nothing; telling it the budget is noise"
    )




@pytest.mark.asyncio
async def test_the_legs_are_not_told_a_budget_they_cannot_spend() -> None:
    """cabal#156 r2 Nit — THE NOTE IS FOR SPENDERS.

    `_aggregate` was excluded from the note on the stated rule that it joins
    outputs and spends nothing, so telling it the budget is noise in a prompt
    that is already long. A leg is a leaf by the same measure — a `Subordinate`
    has no `tool_proxy` and cannot delegate — but it inherited the stamp
    through `**parent_task.metadata` and folded it in via `build_messages`.

    That is the same noise the one aggregate call was spared, multiplied by the
    fan-out width, and the note's own text ("plan the ones you have left") asks
    a leaf to do something it cannot. Not wrong, but not the rule the diff
    states — and a rule applied to one of two identical cases is the kind of
    thing that reads as an oversight later.

    Asserts the plan hop KEEPS it in the same run, so a fix that strips the key
    everywhere cannot pass.
    """
    from cabal.core.guardrails import GuardrailContext
    from cabal.core.guardrails_builtin import DelegationBudgetGuardrail

    primus_client, primus_calls = _client(["a plan", "a summary"])
    sub_client, sub_calls = _client(["impl"])
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
    )
    task = _task_with_ctx("build")
    await DelegationBudgetGuardrail(limit=20).check(
        task, GuardrailContext(persona_name="caller", task_id=task.id, phase="router")
    )

    await mason.handle_task(task)

    leg_system = sub_calls[0]["messages"][0]["content"]
    assert "of 20 used" not in leg_system, (
        "a leg that cannot delegate was handed a delegation budget to plan"
    )
    plan_system = primus_calls[0]["messages"][0]["content"]
    assert "1 of 20 used" in plan_system, "the fix stripped the key from the spender too"


# --- cabal#171: the counter and the check must MEET ---------------------------


@pytest.mark.asyncio
async def test_the_depth_the_guard_sees_is_the_depth_the_fanout_stamped() -> None:
    """THE ASSERTION THAT WOULD HAVE CAUGHT THE PRESENT STATE (cabal#171).

    `next_delegation_depth` stamped a depth on every fan-out leg and nothing
    read it at a decision point: `DelegationDepthGuardrail` ran only inside
    `build_router_input_guardrails`, and a leg goes straight to
    `subord.handle_task` without touching the router. The increment was
    WRITE-ONLY, so cabal's own delegation could not accumulate depth at all.

    Every prior test looked at ONE of those two numbers. This one pins that
    they are the same number, by capturing what the guard was actually handed.
    """
    seen: list[int] = []

    class _Spy(DelegationDepthGuardrail):
        async def check(self, target, ctx):  # type: ignore[override]
            seen.append(int(target.metadata.get(DEPTH_KEY, -1)))
            return await super().check(target, ctx)

    primus_client, _ = _client(["plan", "sum"])
    sub_client, _ = _client(["impl"])
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
        delegation_depth=_Spy(max_depth=8),
    )

    parent = _task()
    parent.metadata = {DEPTH_KEY: 3}
    await mason.handle_task(parent)

    assert seen, "the depth guard was never consulted at the subordinate boundary"
    assert seen == [next_delegation_depth(parent)], (
        f"the guard saw {seen}, the fan-out stamped "
        f"{next_delegation_depth(parent)} — the counter and the check "
        "are looking at different numbers"
    )


@pytest.mark.asyncio
async def test_a_fanout_at_the_depth_limit_is_refused() -> None:
    """And the number being the same must actually STOP something.

    The test above would pass on a guard that is consulted and ignored, so this
    one pins the refusal. A parent arriving at the limit means its legs would
    be one deeper, which is what the guard must catch.
    """
    primus_client, _ = _client(["plan", "sum"])
    sub_client, _ = _client(["impl"])
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
        delegation_depth=DelegationDepthGuardrail(max_depth=2),
    )

    parent = _task()
    parent.metadata = {DEPTH_KEY: 2}
    response = await mason.handle_task(parent)

    # A refused leg fails the fan-out, the same shape a doom-loop block takes.
    assert response.status.state == "failed", (
        "a fan-out whose legs exceed the depth limit was not refused"
    )


@pytest.mark.asyncio
async def test_depth_none_leaves_the_fanout_exactly_as_before() -> None:
    """An operator who disabled depth must see no change (`delegation_depth`
    is None then, and the boundary must not invent a guard)."""
    primus_client, _ = _client(["plan", "sum"])
    sub_client, _ = _client(["impl"])
    mason = MasonPrimusPersona(
        primus_client,
        subordinates={"implementer": _subordinate(sub_client)},
        doom_loop=None,
        delegation_depth=None,
    )
    parent = _task()
    parent.metadata = {DEPTH_KEY: 99}  # far past any limit
    response = await mason.handle_task(parent)
    assert response.status.state == "completed"


def test_the_fanout_checks_guards_in_the_SAME_ORDER_as_the_router() -> None:
    """Neither order was pinned, and they had already drifted (cabal#177).

    `run_guardrails` evaluates the router's list top-to-bottom with
    first-refusal-wins, so list order IS evaluation order, and the router says
    the ordering is deliberate: *"ORDER MATTERS ONLY FOR THE MESSAGE, not for
    safety: the first BLOCK short-circuits, so a tight repeat is reported as a
    doom loop and a paraphrase storm as an exhausted budget."*

    A fan-out loop in a different order does not make anything unsafe — both
    paths still refuse — but it makes them refuse a leg that is BOTH a tight
    repeat and past its depth limit with two different reasons, decided by
    which path dispatched it. That is the kind of disagreement that is invisible
    until someone is reading two logs side by side and cannot explain them.

    SOURCE-LEVEL on both sides, because the thing worth asserting is that the
    two shipped files agree — not that a reproduction of them does.
    """
    import re
    from pathlib import Path

    import cabal.agents.primus as primus_mod
    import cabal.core.app as app_mod

    router_src = Path(app_mod.__file__).read_text()
    primus_src = Path(primus_mod.__file__).read_text()

    names = ("DoomLoopGuardrail", "DelegationDepthGuardrail", "DelegationBudgetGuardrail")

    # THE ORACLE IS THE FUNCTION BODY, NOT THE FILE (#177 r1 Important 2).
    # An earlier version took `router_src.index(f"{n}(")` over the whole of
    # app.py, so ANY earlier mention set the order. The reviewer broke it by
    # adding two lines to `build_router_input_guardrails`'s DOCSTRING, with the
    # fan-out mutated to depth-first: the shipped orders genuinely disagreed and
    # this test went GREEN. The same edit turns it RED with the code correct.
    # A test that reports green while the property it names is false is the same
    # failure class as a counter nothing reads -- which is what this PR exists
    # to close -- so the oracle has to be the statements, not the prose above
    # them.
    start = router_src.index("def build_router_input_guardrails")
    end = router_src.index("\ndef ", start + 1)
    body = router_src[start:end].split('"""', 2)[2]  # code only, no docstring
    router_order = [
        n for _, n in sorted((body.index(f"{n}("), n) for n in names if f"{n}(" in body)
    ]
    assert len(router_order) == len(names), (
        f"expected all of {names} in build_router_input_guardrails, got {router_order}"
    )

    # The fan-out tuple, read as it is written.
    tup = re.search(r"for guard in \(\s*([^)]+?)\s*\):", primus_src, re.S)
    assert tup, "the fan-out guard loop is not where this test expects it"
    attr_to_class = {
        "self._doom_loop": "DoomLoopGuardrail",
        "self._delegation_depth": "DelegationDepthGuardrail",
        "self._delegation_budget": "DelegationBudgetGuardrail",
    }
    fanout_order = [
        attr_to_class[a.strip().rstrip(",")]
        for a in tup.group(1).split("\n")
        if a.strip().rstrip(",") in attr_to_class
    ]

    assert fanout_order == router_order, (
        f"the fan-out checks {fanout_order} but the router checks {router_order}; "
        "the same leg would be refused with two different reasons depending on "
        "which path dispatched it"
    )
