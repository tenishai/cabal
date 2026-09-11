"""Mason primus + Subordinate dispatch tests."""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.personas.mason import MASON_SYSTEM_PROMPT, MasonPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.codes import CODE_POLICY_VIOLATION, reason_code
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.guardrails_builtin import DoomLoopGuardrail
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
async def test_the_fan_out_stamps_and_accumulates_depth() -> None:
    """cabal#144 — depth is a property of the CHAIN, not of one primus's view.

    Inherited-plus-one rather than recomputed, so a chain that leaves this
    process and comes back keeps counting. Without that, a cycle through a
    remote peer resets to 1 on every return and the bound never fires —
    which is the ledger -> gemini-one -> ledger shape from the incident.
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
