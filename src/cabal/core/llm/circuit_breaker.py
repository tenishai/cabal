"""Circuit breaker for the LiteLLM client.

§6.11 — the circuit lives at the CLIENT wrapper, not at the LiteLLM proxy
itself: when proxy errors cluster, we want every persona to back off
together rather than each one waiting for its own threshold.

States:
    CLOSED   — calls pass through; failures counted in a rolling window
    OPEN     — calls fail-fast with CircuitOpenError; reset_timeout starts
               a recovery window
    HALF_OPEN — exactly ONE probe call is allowed; success → CLOSED,
                failure → OPEN with a fresh reset_timeout

Why single-probe HALF_OPEN: if you let N calls through and ALL succeed
on a still-marginally-flaky upstream, you've leaked N failures back to
operators before the breaker re-opens. Single probe = single failed
caller during recovery.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised by the wrapped call when the circuit is OPEN."""


# Clock injection for tests
Clock = Callable[[], float]


@dataclass
class CircuitBreaker:
    """Rolling-window error counter with single-probe HALF_OPEN recovery."""

    failure_threshold: int = 3
    window_seconds: float = 30.0
    reset_seconds: float = 30.0
    clock: Clock = field(default=time.monotonic)

    _failures: deque[float] = field(default_factory=deque, init=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _probe_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> CircuitState:
        return self._state

    def _prune_window(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _can_attempt_probe(self, now: float) -> bool:
        return (
            self._state == CircuitState.OPEN
            and (now - self._opened_at) >= self.reset_seconds
            and not self._probe_in_flight
        )

    def before_call(self) -> None:
        """Throw if the circuit refuses this call right now."""
        now = self.clock()
        if self._state == CircuitState.OPEN:
            if self._can_attempt_probe(now):
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
                return
            raise CircuitOpenError(
                f"circuit OPEN (opened {now - self._opened_at:.1f}s ago, "
                f"reset in {self.reset_seconds - (now - self._opened_at):.1f}s)"
            )
        if self._state == CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                raise CircuitOpenError("circuit HALF_OPEN — probe already in flight")
            self._probe_in_flight = True

    def on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failures.clear()
            self._probe_in_flight = False
            return
        # In CLOSED, success doesn't move the needle — but we DO clean the
        # rolling window so old failures age out.
        self._prune_window(self.clock())

    def on_failure(self) -> None:
        now = self.clock()
        if self._state == CircuitState.HALF_OPEN:
            # Single-probe HALF_OPEN: a probe failure re-OPENs with a fresh
            # reset timer.
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._probe_in_flight = False
            return

        self._failures.append(now)
        self._prune_window(now)
        if len(self._failures) >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._failures.clear()
            self._probe_in_flight = False
