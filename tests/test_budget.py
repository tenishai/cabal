"""Budget guard tests."""

from __future__ import annotations

import pytest

from cabal.core.llm.budget import (
    DEFAULT_TASK_BUDGET,
    BudgetExhaustedError,
    BudgetGuard,
)


def test_default_remaining_is_default_budget() -> None:
    g = BudgetGuard()
    assert g.remaining("t-1") == DEFAULT_TASK_BUDGET


def test_reserve_deducts_from_remaining() -> None:
    g = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    g.reserve("t-1")
    assert g.remaining("t-1") == 90_000


def test_reserve_throws_when_short() -> None:
    g = BudgetGuard(default_budget=5_000, reserve_per_dispatch=10_000)
    with pytest.raises(BudgetExhaustedError, match="need 10000"):
        g.reserve("t-1")


def test_refund_returns_unused_portion() -> None:
    g = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    g.reserve("t-1")  # remaining: 90_000
    g.refund("t-1", used=3_000)  # refund: 7_000
    assert g.remaining("t-1") == 97_000


def test_refund_with_explicit_reserved_amount() -> None:
    g = BudgetGuard(default_budget=100_000)
    g.reserve("t-1", amount=20_000)  # remaining: 80_000
    g.refund("t-1", used=8_000, reserved=20_000)  # refund 12_000
    assert g.remaining("t-1") == 92_000


def test_set_budget_overrides_default() -> None:
    g = BudgetGuard(default_budget=200_000)
    g.set_budget("t-1", 50_000)
    assert g.remaining("t-1") == 50_000
    g.reserve("t-1", amount=30_000)
    assert g.remaining("t-1") == 20_000


def test_reset_drops_tracking() -> None:
    g = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    g.reserve("t-1")
    g.reset("t-1")
    assert g.remaining("t-1") == 100_000


def test_multiple_reserves_compound() -> None:
    g = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    g.reserve("t-1")
    g.reserve("t-1")
    g.reserve("t-1")
    assert g.remaining("t-1") == 70_000


def test_per_task_budgets_isolated() -> None:
    g = BudgetGuard(default_budget=100_000, reserve_per_dispatch=10_000)
    g.reserve("t-1")
    g.reserve("t-2")
    assert g.remaining("t-1") == 90_000
    assert g.remaining("t-2") == 90_000
