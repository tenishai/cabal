"""cabal#11 — AbortGraph + AbortCoordinator (cooperative, in-process)."""

from __future__ import annotations

import asyncio

import pytest

from cabal.core.abort import (
    KIND_LLM,
    KIND_SUBORDINATE,
    AbortCoordinator,
    TaskAborted,
)


@pytest.fixture
def coord() -> AbortCoordinator:
    return AbortCoordinator()


# -- graph construction --------------------------------------------------


def test_register_links_child_to_parent(coord: AbortCoordinator) -> None:
    coord.register("p")
    child = coord.register("c", parent_id="p", kind=KIND_SUBORDINATE)
    assert child.parent_id == "p"
    assert "c" in coord._nodes["p"].children
    assert child.kind == KIND_SUBORDINATE


def test_register_is_idempotent(coord: AbortCoordinator) -> None:
    a = coord.register("p")
    b = coord.register("p")
    assert a is b


def test_complete_unlinks_from_parent(coord: AbortCoordinator) -> None:
    coord.register("p")
    coord.register("c", parent_id="p")
    coord.complete("c")
    assert "c" not in coord._nodes
    assert "c" not in coord._nodes["p"].children


def test_complete_reparents_live_children(coord: AbortCoordinator) -> None:
    coord.register("p")
    coord.register("c", parent_id="p")
    coord.register("g", parent_id="c")
    coord.complete("c")  # c done while g still live
    assert coord._nodes["g"].parent_id == "p"
    assert "g" in coord._nodes["p"].children


# -- transitive abort ----------------------------------------------------


def test_abort_propagates_to_descendants(coord: AbortCoordinator) -> None:
    coord.register("p")
    coord.register("c", parent_id="p")
    coord.register("g", parent_id="c")
    signalled = coord.abort("p", reason="operator")
    assert signalled == 3
    assert coord.is_aborted("p")
    assert coord.is_aborted("c")
    assert coord.is_aborted("g")


def test_abort_fans_out_to_parallel_children(coord: AbortCoordinator) -> None:
    coord.register("p")
    for i in range(4):
        coord.register(f"c{i}", parent_id="p")
    assert coord.abort("p") == 5
    assert all(coord.is_aborted(f"c{i}") for i in range(4))


def test_abort_only_subtree_not_siblings(coord: AbortCoordinator) -> None:
    coord.register("root")
    coord.register("a", parent_id="root")
    coord.register("b", parent_id="root")
    coord.register("a1", parent_id="a")
    coord.abort("a")
    assert coord.is_aborted("a") and coord.is_aborted("a1")
    assert not coord.is_aborted("b")
    assert not coord.is_aborted("root")


def test_child_registered_under_aborted_parent_inherits(coord: AbortCoordinator) -> None:
    # The race: parent aborted, then a late child registers under it.
    coord.register("p")
    coord.abort("p", reason="timeout")
    late = coord.register("late", parent_id="p")
    assert late.aborted
    assert late.reason == "timeout"


def test_abort_records_first_reason_only(coord: AbortCoordinator) -> None:
    coord.register("p")
    coord.abort("p", reason="first")
    coord.abort("p", reason="second")
    assert coord._nodes["p"].reason == "first"


def test_abort_unknown_node_is_noop(coord: AbortCoordinator) -> None:
    assert coord.abort("ghost") == 0


def test_raise_if_aborted(coord: AbortCoordinator) -> None:
    coord.register("p")
    coord.raise_if_aborted("p")  # not aborted → no raise
    coord.raise_if_aborted("untracked")  # unknown → no raise
    coord.abort("p", reason="boom")
    with pytest.raises(TaskAborted) as ei:
        coord.raise_if_aborted("p")
    assert ei.value.node_id == "p"
    assert ei.value.reason == "boom"


# -- guard (race an await against abort) ---------------------------------


async def test_guard_returns_result_when_not_aborted(coord: AbortCoordinator) -> None:
    coord.register("n", kind=KIND_LLM)

    async def work() -> str:
        await asyncio.sleep(0)
        return "ok"

    assert await coord.guard("n", work()) == "ok"


async def test_guard_untracked_node_plain_awaits(coord: AbortCoordinator) -> None:
    async def work() -> int:
        return 42

    assert await coord.guard("missing", work()) == 42


async def test_guard_aborts_in_flight_work(coord: AbortCoordinator) -> None:
    coord.register("n", kind=KIND_LLM)
    completed = False

    async def slow() -> str:
        nonlocal completed
        await asyncio.sleep(5)
        completed = True
        return "done"

    async def aborter() -> None:
        await asyncio.sleep(0.02)
        coord.abort("n", reason="cancelled")

    with pytest.raises(TaskAborted) as ei:
        await asyncio.gather(coord.guard("n", slow()), aborter())
    assert ei.value.reason == "cancelled"
    assert completed is False  # in-flight work was abandoned, not run to completion


async def test_guard_fast_path_when_already_aborted(coord: AbortCoordinator) -> None:
    coord.register("n")
    coord.abort("n", reason="pre")
    ran = False

    async def work() -> None:
        nonlocal ran
        ran = True

    with pytest.raises(TaskAborted):
        await coord.guard("n", work())
    assert ran is False  # never started


# -- observability -------------------------------------------------------


def test_dump_subtree_shape(coord: AbortCoordinator) -> None:
    coord.register("p", kind="dispatch")
    coord.register("c", parent_id="p", kind=KIND_LLM)
    coord.abort("c", reason="x")
    tree = coord.dump("p")
    assert tree["node_id"] == "p"
    assert tree["aborted"] is False
    assert len(tree["children"]) == 1
    child = tree["children"][0]
    assert child["node_id"] == "c"
    assert child["kind"] == KIND_LLM
    assert child["aborted"] is True
    assert child["reason"] == "x"


def test_dump_all_roots(coord: AbortCoordinator) -> None:
    coord.register("r1")
    coord.register("r2")
    coord.register("r1c", parent_id="r1")
    roots = coord.dump()
    assert {r["node_id"] for r in roots} == {"r1", "r2"}


# -- wiring: handle_task honors the coordinator --------------------------


def _persona(coord: AbortCoordinator, acompletion_fn) -> object:
    from cabal.agents.llm_persona import LLMPersona
    from cabal.core.hooks.registry import HookRegistry
    from cabal.core.llm.litellm_client import LiteLLMClient

    return LLMPersona(
        name="x",
        model="m",
        llm_client=LiteLLMClient(acompletion_fn=acompletion_fn, hook_registry=HookRegistry()),
        system_prompt="x",
        abort_coordinator=coord,
    )


def _abort_task(task_id: str = "t-abort") -> object:
    from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart

    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted", message=Message(role="user", parts=[TextPart(text="hi")])
        ),
    )


async def test_handle_task_abandons_in_flight_llm_call(coord: AbortCoordinator) -> None:
    """An operator abort mid-dispatch cancels the in-flight LLM call cleanly."""
    started = asyncio.Event()
    completed = False

    async def slow(model, messages, **kwargs):  # noqa: ANN001, ARG001
        nonlocal completed
        started.set()
        await asyncio.sleep(5)
        completed = True
        return {"choices": [{"message": {"content": "late"}}]}

    persona = _persona(coord, slow)
    coord.register("t-abort")  # the router would do this; here we stand in for it
    run = asyncio.ensure_future(persona.handle_task(_abort_task()))

    await asyncio.wait_for(started.wait(), timeout=1.0)  # LLM call is in-flight
    assert coord.abort("t-abort", reason="operator") == 1

    with pytest.raises(TaskAborted):
        await run
    assert completed is False  # the LLM call was abandoned, not awaited to completion


async def test_handle_task_completes_when_not_aborted(coord: AbortCoordinator) -> None:
    """A registered-but-not-aborted task runs normally through the guards."""

    async def quick(model, messages, **kwargs):  # noqa: ANN001, ARG001
        return {"choices": [{"message": {"content": "done"}}]}

    persona = _persona(coord, quick)
    coord.register("t-abort")
    result = await persona.handle_task(_abort_task())
    assert result.status.state == "completed"


async def test_handle_task_unregistered_is_unaffected(coord: AbortCoordinator) -> None:
    """With no node registered (default standalone persona), guards are no-ops."""

    async def quick(model, messages, **kwargs):  # noqa: ANN001, ARG001
        return {"choices": [{"message": {"content": "done"}}]}

    persona = _persona(coord, quick)  # note: never register the task id
    result = await persona.handle_task(_abort_task())
    assert result.status.state == "completed"


async def test_handle_task_ends_session_on_abort(coord: AbortCoordinator) -> None:
    """Aborting mid-dispatch closes the techne session cleanly (no leak)."""
    from dataclasses import dataclass, field

    @dataclass
    class RecordingTracker:
        ends: list[dict] = field(default_factory=list)

        def start(self, *, external_id, agent_identity):  # noqa: ANN001, ANN204, ARG002
            return "sess-1"

        def end(self, *, session_id, summary):  # noqa: ANN001, ANN204
            self.ends.append({"session_id": session_id, "summary": summary})

    started = asyncio.Event()

    async def slow(model, messages, **kwargs):  # noqa: ANN001, ARG001
        started.set()
        await asyncio.sleep(5)
        return {"choices": [{"message": {"content": "late"}}]}

    tracker = RecordingTracker()
    persona = _persona(coord, slow)
    persona.session_tracker = tracker
    coord.register("t-abort")
    run = asyncio.ensure_future(persona.handle_task(_abort_task()))

    await asyncio.wait_for(started.wait(), timeout=1.0)
    coord.abort("t-abort", reason="operator")
    with pytest.raises(TaskAborted):
        await run
    assert tracker.ends == [{"session_id": "sess-1", "summary": "[canceled] aborted"}]


async def test_budget_trip_aborts_descendants(coord: AbortCoordinator) -> None:
    """A budget exhaustion on a task aborts its descendants (cabal#11 hook)."""
    from cabal.core.hooks.registry import HookRegistry
    from cabal.core.llm.budget import BudgetGuard
    from cabal.core.llm.litellm_client import LiteLLMClient, LLMUnavailableError

    async def never_called(model, messages, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("budget trip should short-circuit before the LLM call")

    client = LiteLLMClient(
        acompletion_fn=never_called,
        hook_registry=HookRegistry(),
        budget=BudgetGuard(default_budget=0),  # any reserve trips immediately
        abort_coordinator=coord,
    )
    coord.register("parent")
    coord.register("child", parent_id="parent")  # e.g. a Rook subordinate

    with pytest.raises(LLMUnavailableError):
        await client.acompletion(model="m", messages=[], task_id="parent")

    assert coord.is_aborted("parent")
    assert coord.is_aborted("child")  # descendant cancelled by the trip
