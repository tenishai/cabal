"""Lifecycle hook framework.

The broker, router, and agent runtime fire named hooks at well-known
points: `session_start`, `phase_transition`, `litellm_pre/post`,
`subordinate_dispatched/returned`, `tool_use`, `session_end`, `agent_crash`.

Each hook can have multiple registered handlers. Handlers declare their
semantics:

    sync + on_failure="fail" — the handler's exception bubbles up and
        aborts the parent flow. Use for invariants that must hold (e.g.
        budget guard).
    sync + on_failure="log" — exceptions are logged and swallowed; the
        parent flow continues. Default.
    async — fire-and-forget; failures logged but never block. Default
        for `agent_crash` and any handler whose side effect is observation
        only (Langfuse trace export, etc.).

Sync hooks are subject to `timeout_seconds`; if they exceed it the timeout
is treated like an exception under the configured `on_failure` rule.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# Canonical hook names — adding to this list = a contract change.
class HookName(StrEnum):
    SESSION_START = "session_start"
    PHASE_TRANSITION = "phase_transition"
    LITELLM_PRE = "litellm_pre"
    LITELLM_POST = "litellm_post"
    SUBORDINATE_DISPATCHED = "subordinate_dispatched"
    SUBORDINATE_RETURNED = "subordinate_returned"
    TOOL_USE = "tool_use"
    SESSION_END = "session_end"
    AGENT_CRASH = "agent_crash"


HookFn = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class HookConfig:
    mode: str = "sync"  # "sync" | "async"
    timeout_seconds: float = 5.0
    on_failure: str = "log"  # "log" | "fail"


@dataclass(slots=True)
class _Registration:
    fn: HookFn
    config: HookConfig
    label: str


class HookError(RuntimeError):
    """Raised when a sync+fail handler fails or times out."""


@dataclass
class HookRegistry:
    """Maps hook name → ordered list of registered handlers."""

    _handlers: dict[str, list[_Registration]] = field(default_factory=dict)

    def register(
        self,
        name: str,
        fn: HookFn,
        *,
        mode: str = "sync",
        timeout_seconds: float = 5.0,
        on_failure: str = "log",
        label: str | None = None,
    ) -> None:
        cfg = HookConfig(mode=mode, timeout_seconds=timeout_seconds, on_failure=on_failure)
        self._handlers.setdefault(name, []).append(
            _Registration(fn=fn, config=cfg, label=label or fn.__name__)
        )

    def handlers_for(self, name: str) -> list[_Registration]:
        return list(self._handlers.get(name, ()))

    async def fire(self, name: str, payload: dict[str, Any]) -> None:
        """Dispatch a hook to all registered handlers.

        Sync handlers run inline; async handlers are spawned as tasks
        (fire-and-forget, with exception logging).
        """
        for reg in self._handlers.get(name, ()):
            if reg.config.mode == "async":
                asyncio.create_task(self._run_async(name, reg, payload))
            else:
                await self._run_sync(name, reg, payload)

    async def _run_sync(
        self, name: str, reg: _Registration, payload: dict[str, Any]
    ) -> None:
        try:
            result = reg.fn(payload)
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=reg.config.timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._handle_failure(name, reg, exc, kind="timeout")
        except Exception as exc:  # noqa: BLE001
            self._handle_failure(name, reg, exc, kind="exception")

    async def _run_async(
        self, name: str, reg: _Registration, payload: dict[str, Any]
    ) -> None:
        try:
            result = reg.fn(payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "hook.async_handler_failed",
                hook=name,
                label=reg.label,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _handle_failure(
        self, name: str, reg: _Registration, exc: Exception, *, kind: str
    ) -> None:
        log.warning(
            "hook.handler_failed",
            hook=name,
            label=reg.label,
            kind=kind,
            on_failure=reg.config.on_failure,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        if reg.config.on_failure == "fail":
            raise HookError(
                f"hook {name!r} handler {reg.label!r} {kind}: {exc}"
            ) from exc
