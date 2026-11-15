"""Issue 394: one token budget for a primus's whole fan-out, and one per leg.

Before, each leg spent against a fresh default budget under its own sub-task
id, so N legs could spend N budgets and nothing totalled them.
"""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.personas.wright import WrightPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.codes import CODE_BUDGET_EXHAUSTED, reason_code
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.budget import BudgetExhaustedError, BudgetGuard
from cabal.core.llm.litellm_client import LiteLLMClient

USED = 1_000  # tokens each stub call reports


def _client(guard: BudgetGuard, *, used: int = USED) -> tuple[LiteLLMClient, list[str]]:
    """A stub client that records each call's task id and reports `used` tokens."""
    task_ids: list[str] = []

    async def fn(model: str, messages: list, **kwargs: Any):
        return {
            "choices": [{"message": {"content": "done"}}],
            "usage": {"prompt_tokens": used, "completion_tokens": 0, "total_tokens": used},
        }

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry(), budget=guard)
    real = client.acompletion

    async def spy(*args: Any, **kwargs: Any):
        task_ids.append(kwargs.get("task_id"))
        return await real(*args, **kwargs)

    client.acompletion = spy  # type: ignore[method-assign]
    return client, task_ids


def _task(task_id: str = "t-fan", metadata: dict[str, Any] | None = None) -> Task:
    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="build it")]),
        ),
        metadata=metadata or {},
    )


def _wright(client: LiteLLMClient, roles: tuple[str, ...], **kw: Any) -> WrightPrimusPersona:
    subs = {
        r: Subordinate(primus="wright", role=r, llm_client=client, model="m") for r in roles
    }
    return WrightPrimusPersona(client, subs, doom_loop=None, **kw)


# ---- BudgetGuard ------------------------------------------------------------


def test_open_leg_carves_the_allotment_from_the_root() -> None:
    g = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    g.set_budget("root", 50_000)
    g.open_leg("root.a", "root", 20_000)
    assert g.remaining("root") == 30_000
    assert g.remaining("root.a") == 20_000


def test_open_leg_refuses_and_changes_nothing_when_the_root_is_short() -> None:
    g = BudgetGuard()
    g.set_budget("root", 15_000)
    with pytest.raises(BudgetExhaustedError):
        g.open_leg("root.a", "root", 20_000)
    assert g.remaining("root") == 15_000
    assert not g.has_budget("root.a")


def test_close_leg_returns_the_unused_allotment_and_reports_spend() -> None:
    g = BudgetGuard(reserve_per_dispatch=10_000)
    g.set_budget("root", 50_000)
    g.open_leg("root.a", "root", 20_000)
    g.reserve("root.a")
    g.refund("root.a", used=3_000)
    assert g.close_leg("root.a") == 3_000
    assert g.remaining("root") == 47_000
    assert g.spent("root") == 3_000
    assert not g.has_budget("root.a")


def test_a_leg_cannot_spend_past_its_allotment() -> None:
    g = BudgetGuard(reserve_per_dispatch=10_000)
    g.set_budget("root", 100_000)
    g.open_leg("root.a", "root", 15_000)
    g.reserve("root.a")
    g.refund("root.a", used=6_000)
    with pytest.raises(BudgetExhaustedError):
        g.reserve("root.a")  # 9_000 left, a call reserves 10_000
    assert g.remaining("root") == 85_000  # the root kept its share


def test_a_call_over_its_reservation_is_charged_the_overage() -> None:
    g = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    g.reserve("t")
    g.refund("t", used=25_000)
    assert g.remaining("t") == 75_000
    assert g.spent("t") == 25_000


def test_an_overspent_leg_charges_the_root_on_close() -> None:
    g = BudgetGuard(reserve_per_dispatch=10_000)
    g.set_budget("root", 50_000)
    g.open_leg("root.a", "root", 20_000)
    g.reserve("root.a")
    g.refund("root.a", used=30_000)  # 10_000 past its allotment
    g.close_leg("root.a")
    assert g.remaining("root") == 20_000


def test_affordable_legs_keeps_room_for_the_root() -> None:
    g = BudgetGuard()
    g.set_budget("root", 70_000)
    assert g.affordable_legs("root", 20_000) == 3
    assert g.affordable_legs("root", 20_000, keep=15_000) == 2
    assert g.affordable_legs("root", 0) == 0


def test_reset_drops_spend_and_leg_links() -> None:
    g = BudgetGuard(reserve_per_dispatch=10_000)
    g.set_budget("root", 50_000)
    g.reserve("root")
    g.refund("root", used=1_000)
    g.reset("root")
    assert g.spent("root") == 0
    assert not g.has_budget("root")


# ---- primus fan-out ---------------------------------------------------------


@pytest.mark.asyncio
async def test_legs_spend_against_the_root_not_fresh_budgets() -> None:
    guard = BudgetGuard(default_budget=200_000, reserve_per_dispatch=10_000)
    client, ids = _client(guard)
    seen: dict[str, int] = {}
    real_close = guard.close_leg

    def close(leg_id: str) -> int:
        seen[leg_id] = guard.remaining(leg_id)
        return real_close(leg_id)

    guard.close_leg = close  # type: ignore[method-assign]
    wright = _wright(
        client, ("implementer", "tester"), leg_token_budget=30_000, fanout_token_budget=100_000
    )
    out = await wright.handle_task(_task())
    assert out.status.state == "completed"
    # Each leg ran on its 30_000 allotment and spent one call.
    assert len(seen) == 2
    assert all(left == 30_000 - USED for left in seen.values())
    # Plan, two legs and the aggregate all called the model, legs under their own ids.
    assert ids[0] == "t-fan" and ids[-1] == "t-fan"
    assert sorted(i.split(".")[1].split("-")[0] for i in ids[1:-1]) == ["implementer", "tester"]
    # The root's tracking is dropped when the fan-out ends.
    assert not guard.has_budget("t-fan")


@pytest.mark.asyncio
async def test_a_fanout_that_cannot_cover_every_worker_is_refused_before_any_starts() -> None:
    guard = BudgetGuard(default_budget=200_000, reserve_per_dispatch=10_000)
    client, ids = _client(guard)
    # Two legs and a reviewer at 30_000 each, plus 10_000 kept for the
    # aggregate, is 100_000; the plan call leaves 89_000.
    wright = _wright(
        client,
        ("implementer", "tester", "reviewer"),
        leg_token_budget=30_000,
        fanout_token_budget=90_000,
    )
    out = await wright.handle_task(_task())
    assert out.status.state == "failed"
    assert reason_code(out) == CODE_BUDGET_EXHAUSTED
    assert ids == ["t-fan"]  # only the plan call ran


@pytest.mark.asyncio
async def test_the_reviewer_draws_an_allotment_too() -> None:
    guard = BudgetGuard(default_budget=200_000, reserve_per_dispatch=10_000)
    client, ids = _client(guard)
    wright = _wright(
        client,
        ("implementer", "reviewer"),
        leg_token_budget=30_000,
        fanout_token_budget=80_000,  # 79_000 after the plan: 2 x 30_000 + 10_000 fits
    )
    opened: list[str] = []
    real_open = guard.open_leg

    def open_leg(leg_id: str, root_id: str, allotment: int) -> None:
        opened.append(leg_id.split(".")[1].split("-")[0])
        real_open(leg_id, root_id, allotment)

    guard.open_leg = open_leg  # type: ignore[method-assign]
    out = await wright.handle_task(_task())
    assert out.status.state == "completed"
    assert opened == ["implementer", "reviewer"]


@pytest.mark.asyncio
async def test_default_budgets_cover_the_old_spend() -> None:
    """Unset, the fan-out budget is one default plus one allotment per worker,
    so a primus that ran before still runs."""
    guard = BudgetGuard(default_budget=50_000, reserve_per_dispatch=10_000)
    client, _ = _client(guard)
    wright = _wright(client, ("implementer", "tester", "reviewer"))
    budgets: list[int] = []
    real_set = guard.set_budget

    def set_budget(task_id: str, budget: int) -> None:
        budgets.append(budget)
        real_set(task_id, budget)

    guard.set_budget = set_budget  # type: ignore[method-assign]
    out = await wright.handle_task(_task())
    assert out.status.state == "completed"
    assert budgets[0] == 50_000 + 3 * 50_000


@pytest.mark.asyncio
async def test_a_budget_already_set_on_the_root_is_kept() -> None:
    guard = BudgetGuard(default_budget=200_000, reserve_per_dispatch=10_000)
    guard.set_budget("t-fan", 20_000)
    client, ids = _client(guard)
    wright = _wright(client, ("implementer",), leg_token_budget=15_000)
    out = await wright.handle_task(_task())
    # 19_000 after the plan cannot cover 15_000 plus the 10_000 aggregate.
    assert reason_code(out) == CODE_BUDGET_EXHAUSTED
    assert ids == ["t-fan"]


@pytest.mark.asyncio
async def test_a_leg_does_not_inherit_the_roots_token_budget_override() -> None:
    """A leg that inherited `cabal_token_budget` would reset its own budget to
    it and spend outside its allotment."""
    guard = BudgetGuard(default_budget=200_000, reserve_per_dispatch=10_000)
    client, _ = _client(guard)
    wright = _wright(client, ("implementer",), leg_token_budget=30_000)
    leg_meta: list[dict[str, Any]] = []
    sub = wright.subordinates["implementer"]
    real = sub.handle_task

    async def handle(task: Task, **kw: Any) -> Task:
        leg_meta.append(dict(task.metadata))
        return await real(task, **kw)

    sub.handle_task = handle  # type: ignore[method-assign]
    await wright.handle_task(_task(metadata={"cabal_token_budget": 10_000_000}))
    assert "cabal_token_budget" not in leg_meta[0]


@pytest.mark.asyncio
async def test_without_a_budget_guard_the_fanout_runs_as_before() -> None:
    async def fn(model: str, messages: list, **kwargs: Any):
        return {"choices": [{"message": {"content": "done"}}]}

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    wright = _wright(client, ("implementer", "reviewer"), fanout_token_budget=1)
    out = await wright.handle_task(_task())
    assert out.status.state == "completed"


def test_identity_refuses_a_non_positive_token_budget() -> None:
    from pydantic import ValidationError

    from cabal.config.identity import PersonaConfig

    with pytest.raises(ValidationError):
        PersonaConfig(role="primus", leg_token_budget_override=0)
    with pytest.raises(ValidationError):
        PersonaConfig(role="primus", fanout_token_budget_override=-5)
