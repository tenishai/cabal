"""Per-task token budget reservation (§6.8).

Each Task starts with a token budget — default `DEFAULT_BUDGET` from
identity.toml. Each dispatch reserves a fixed `RESERVE_PER_DISPATCH`
chunk up-front; if the actual usage was less, the remainder is refunded.
If the budget can't cover the reservation, dispatch fails with
BUDGET_EXHAUSTED.

The reserve-then-refund pattern handles the common case where you don't
know up-front how many tokens a single LLM call will consume.

Fan-out legs (issue 394). A primus's leg used to get a fresh budget under its
own sub-task id, so N legs could spend N budgets and nothing totalled them. A
leg is now opened with `open_leg`, which carves a fixed allotment out of the
root task's remaining budget. The leg's calls draw on its allotment, so one leg
can't eat the whole fan-out, and the root's remaining budget is the ceiling for
the whole fan-out: when it can't cover another allotment, the leg isn't
started. `close_leg` returns the unused part of the allotment to the root.
`spent` reports the tokens each task's calls measured, rolled up to the root.
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
    #: leg id -> root task id, for legs opened with `open_leg`.
    _root_of: dict[str, str] = field(default_factory=dict)
    #: Tokens the calls actually used, as measured on the response, per task.
    #: A leg's spend is also added to its root's.
    _spent: dict[str, int] = field(default_factory=dict)

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
        """Settle an earlier reservation against what the call used.

        The unused part of the reservation goes back to remaining. A call that
        used MORE than it reserved is charged the difference too (issue 394):
        before, the overage was free, so a budget could be outspent by any call
        larger than the reservation. Remaining can then go below zero, and the
        next reservation fails.
        """
        original = reserved if reserved is not None else self.reserve_per_dispatch
        used = max(0, used)
        self._record_spend(task_id, used)
        delta = original - used
        self._remaining[task_id] = (
            self._remaining.get(task_id, self.default_budget) + delta
        )
        self._reserved[task_id] = max(0, self._reserved.get(task_id, 0) - original)
        if delta:
            log.debug(
                "budget.settled",
                task_id=task_id,
                used=used,
                returned=delta,
                remaining=self._remaining[task_id],
            )

    def _record_spend(self, task_id: str, used: int) -> None:
        if not used:
            return
        self._spent[task_id] = self._spent.get(task_id, 0) + used
        root = self._root_of.get(task_id)
        if root is not None:
            self._spent[root] = self._spent.get(root, 0) + used

    def spent(self, task_id: str) -> int:
        """Tokens measured on `task_id`'s calls (for a root, including its legs')."""
        return self._spent.get(task_id, 0)

    def has_budget(self, task_id: str) -> bool:
        """Whether `task_id` has a budget set or spent against, rather than the default."""
        return task_id in self._remaining

    def affordable_legs(self, root_id: str, allotment: int, *, keep: int = 0) -> int:
        """How many legs of `allotment` tokens the root can still open, leaving `keep`
        tokens for the root's own calls (a primus's aggregate step)."""
        if allotment <= 0:
            return 0
        return max(0, self.remaining(root_id) - keep) // allotment

    def open_leg(self, leg_id: str, root_id: str, allotment: int) -> None:
        """Start a leg: move `allotment` tokens from the root's remaining budget to the
        leg's. Raises BudgetExhaustedError, and changes nothing, if the root can't cover it."""
        remaining = self.remaining(root_id)
        if remaining < allotment:
            raise BudgetExhaustedError(
                f"task {root_id!r}: a worker needs {allotment} tokens, "
                f"only {remaining} left for the fan-out"
            )
        self._remaining[root_id] = remaining - allotment
        self._remaining[leg_id] = allotment
        self._root_of[leg_id] = root_id
        log.debug(
            "budget.leg_opened",
            leg=leg_id,
            root=root_id,
            allotment=allotment,
            root_remaining=self._remaining[root_id],
        )

    def close_leg(self, leg_id: str) -> int:
        """End a leg: return its unused allotment to the root and drop its tracking.
        Returns the tokens the leg spent. A leg that was never opened returns 0."""
        root = self._root_of.pop(leg_id, None)
        left = self._remaining.pop(leg_id, 0)
        self._reserved.pop(leg_id, None)
        spent = self._spent.pop(leg_id, 0)
        if root is not None:
            self._remaining[root] = self.remaining(root) + left
            log.debug("budget.leg_closed", leg=leg_id, root=root, spent=spent, returned=left)
        return spent

    def set_budget(self, task_id: str, budget: int) -> None:
        """Override per-task budget (e.g., from task.metadata.cabal_token_budget)."""
        self._remaining[task_id] = budget

    def reset(self, task_id: str) -> None:
        """Drop tracking for a terminated task."""
        self._remaining.pop(task_id, None)
        self._reserved.pop(task_id, None)
        self._spent.pop(task_id, None)
        self._root_of.pop(task_id, None)
