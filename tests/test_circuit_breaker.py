"""Circuit breaker state-machine tests."""

from __future__ import annotations

import pytest

from cabal.core.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def tick(self, dt: float) -> None:
        self.t += dt

    def __call__(self) -> float:
        return self.t


def test_starts_closed() -> None:
    cb = CircuitBreaker(clock=FakeClock())
    assert cb.state == CircuitState.CLOSED
    cb.before_call()
    cb.on_success()


def test_opens_after_threshold_failures() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, clock=clock)
    for _ in range(3):
        cb.before_call()
        cb.on_failure()
    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.before_call()


def test_failures_outside_window_dont_count() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, window_seconds=10.0, clock=clock)
    cb.before_call(); cb.on_failure()
    clock.tick(11.0)
    cb.before_call(); cb.on_failure()
    cb.before_call(); cb.on_failure()
    # Only 2 failures inside window
    assert cb.state == CircuitState.CLOSED


def test_success_in_closed_does_not_reset_window() -> None:
    """A single success doesn't clear all prior failures (it just ages them)."""
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, window_seconds=10.0, clock=clock)
    cb.before_call(); cb.on_failure()
    cb.before_call(); cb.on_failure()
    cb.before_call(); cb.on_success()
    # Two failures still in window
    cb.before_call(); cb.on_failure()
    assert cb.state == CircuitState.OPEN


def test_half_open_after_reset_timeout() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=2, reset_seconds=5.0, clock=clock)
    cb.before_call(); cb.on_failure()
    cb.before_call(); cb.on_failure()
    assert cb.state == CircuitState.OPEN
    # Before reset_seconds: still OPEN
    clock.tick(4.0)
    with pytest.raises(CircuitOpenError):
        cb.before_call()
    # After reset_seconds: one HALF_OPEN probe allowed
    clock.tick(2.0)
    cb.before_call()
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_single_probe_success_closes() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=2, reset_seconds=5.0, clock=clock)
    cb.before_call(); cb.on_failure()
    cb.before_call(); cb.on_failure()
    clock.tick(6.0)
    cb.before_call()
    assert cb.state == CircuitState.HALF_OPEN
    cb.on_success()
    assert cb.state == CircuitState.CLOSED


def test_half_open_probe_failure_reopens() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=2, reset_seconds=5.0, clock=clock)
    cb.before_call(); cb.on_failure()
    cb.before_call(); cb.on_failure()
    clock.tick(6.0)
    cb.before_call()
    cb.on_failure()
    assert cb.state == CircuitState.OPEN
    # Reset timer restarted from now, so within reset_seconds we're OPEN
    with pytest.raises(CircuitOpenError):
        cb.before_call()


def test_half_open_only_allows_single_concurrent_probe() -> None:
    """While a probe is in flight, a second call is refused."""
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=2, reset_seconds=5.0, clock=clock)
    cb.before_call(); cb.on_failure()
    cb.before_call(); cb.on_failure()
    clock.tick(6.0)
    cb.before_call()  # probe acquires the slot
    with pytest.raises(CircuitOpenError):
        cb.before_call()  # second concurrent attempt refused
