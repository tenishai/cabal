"""Lifecycle hook registry tests."""

from __future__ import annotations

import asyncio

import pytest

from cabal.core.hooks.registry import HookError, HookName, HookRegistry


@pytest.mark.asyncio
async def test_sync_handler_runs_inline() -> None:
    reg = HookRegistry()
    calls: list[dict] = []

    def handler(payload: dict) -> None:
        calls.append(payload)

    reg.register(HookName.SESSION_START, handler)
    await reg.fire(HookName.SESSION_START, {"task_id": "t-1"})
    assert calls == [{"task_id": "t-1"}]


@pytest.mark.asyncio
async def test_multiple_handlers_run_in_order() -> None:
    reg = HookRegistry()
    order: list[str] = []

    reg.register(HookName.SESSION_START, lambda p: order.append("a"))
    reg.register(HookName.SESSION_START, lambda p: order.append("b"))
    reg.register(HookName.SESSION_START, lambda p: order.append("c"))
    await reg.fire(HookName.SESSION_START, {})
    assert order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_sync_log_swallows_exceptions() -> None:
    reg = HookRegistry()

    def boom(payload: dict) -> None:
        raise ValueError("nope")

    reg.register(HookName.SESSION_START, boom, on_failure="log")
    # Should NOT raise
    await reg.fire(HookName.SESSION_START, {})


@pytest.mark.asyncio
async def test_sync_fail_reraises() -> None:
    reg = HookRegistry()

    def boom(payload: dict) -> None:
        raise ValueError("nope")

    reg.register(HookName.SESSION_START, boom, on_failure="fail")
    with pytest.raises(HookError, match="exception"):
        await reg.fire(HookName.SESSION_START, {})


@pytest.mark.asyncio
async def test_sync_timeout_is_failure() -> None:
    reg = HookRegistry()

    async def slow(payload: dict) -> None:
        await asyncio.sleep(1.0)

    reg.register(
        HookName.SESSION_START,
        slow,
        timeout_seconds=0.05,
        on_failure="fail",
    )
    with pytest.raises(HookError, match="timeout"):
        await reg.fire(HookName.SESSION_START, {})


@pytest.mark.asyncio
async def test_async_handler_does_not_block_caller() -> None:
    reg = HookRegistry()
    event = asyncio.Event()

    async def slow(payload: dict) -> None:
        await asyncio.sleep(0.2)
        event.set()

    reg.register(HookName.AGENT_CRASH, slow, mode="async")
    await reg.fire(HookName.AGENT_CRASH, {})
    # fire() returned immediately; the handler hasn't completed yet
    assert not event.is_set()
    await asyncio.sleep(0.3)
    assert event.is_set()


@pytest.mark.asyncio
async def test_async_handler_failure_logged_not_raised() -> None:
    reg = HookRegistry()

    async def crash(payload: dict) -> None:
        raise RuntimeError("boom")

    reg.register(HookName.AGENT_CRASH, crash, mode="async")
    # Must not raise
    await reg.fire(HookName.AGENT_CRASH, {})
    # Give the task a chance to actually run + log
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_handlers_for_returns_registrations() -> None:
    reg = HookRegistry()
    reg.register(HookName.LITELLM_PRE, lambda p: None, label="a")
    reg.register(HookName.LITELLM_PRE, lambda p: None, label="b")
    assert [h.label for h in reg.handlers_for(HookName.LITELLM_PRE)] == ["a", "b"]
    assert reg.handlers_for(HookName.LITELLM_POST) == []
