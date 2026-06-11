"""Per-task token budget reservation (§6.8).

Each Task starts with a token budget — default `DEFAULT_BUDGET` from
identity.toml. Each dispatch reserves a fixed `RESERVE_PER_DISPATCH`
chunk up-front; if the actual usage was less, the remainder is refunded.
If the budget can't cover the reservation, dispatch fails with
BUDGET_EXHAUSTED.

The reserve-then-refund pattern handles the common case where you don't
know up-front how many tokens a single LLM call will consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


DEFAULT_TASK_BUDGET = 200_000
RESERVE_PER_DISPATCH = 50_000


class BudgetExhaustedError(RuntimeError):
    """Raised when a reservation can't fit in the remaining budget."""


@dataclass
class BudgetGuard:
    """Per-task token budgets, kept in memory.

    Persistence is a Phase 3 concern (Litestream survives a crash anyway;
    a long-running Task that overflows the budget can be resumed with a
    fresh budget via session_token).
    """

    default_budget: int = DEFAULT_TASK_BUDGET
    reserve_per_dispatch: int = RESERVE_PER_DISPATCH
    _remaining: dict[str, int] = field(default_factory=dict)
    _reserved: dict[str, int] = field(default_factory=dict)

    def remaining(self, task_id: str) -> int:
        return self._remaining.get(task_id, self.default_budget)

    def reserve(self, task_id: str, amount: int | None = None) -> int:
        """Reserve `amount` (or RESERVE_PER_DISPATCH) tokens for task_id.

        Returns the amount reserved on success. Raises BudgetExhaustedError
        if remaining < amount.
        """
        amount = amount if amount is not None else self.reserve_per_dispatch
        remaining = self._remaining.get(task_id, self.default_budget)
        if remaining < amount:
            raise BudgetExhaustedError(
                f"task {task_id!r}: need {amount} tokens, only {remaining} remaining"
            )
        self._remaining[task_id] = remaining - amount
        self._reserved[task_id] = self._reserved.get(task_id, 0) + amount
        log.debug(
            "budget.reserved",
            task_id=task_id,
            amount=amount,
            remaining=self._remaining[task_id],
        )
        return amount

    def refund(self, task_id: str, used: int, reserved: int | None = None) -> None:
        """Refund the unused portion of an earlier reservation.

        `used` is what actually got consumed; the difference between the
        original reservation and `used` is returned to remaining.
        """
        original = reserved if reserved is not None else self.reserve_per_dispatch
        refund = max(0, original - used)
        self._remaining[task_id] = (
            self._remaining.get(task_id, self.default_budget) + refund
        )
        self._reserved[task_id] = max(0, self._reserved.get(task_id, 0) - original)
        if refund > 0:
            log.debug(
                "budget.refunded",
                task_id=task_id,
                used=used,
                refund=refund,
                remaining=self._remaining[task_id],
            )

    def set_budget(self, task_id: str, budget: int) -> None:
        """Override per-task budget (e.g., from task.metadata.cabal_token_budget)."""
        self._remaining[task_id] = budget

    def reset(self, task_id: str) -> None:
        """Drop tracking for a terminated task."""
        self._remaining.pop(task_id, None)
        self._reserved.pop(task_id, None)
