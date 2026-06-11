"""Trace exporter — hook-driven observability.

Registers handlers for SESSION_START / SESSION_END / LITELLM_PRE /
LITELLM_POST and forwards the events to a `TraceClient` (Langfuse,
OTLP, or any other backend that implements the Protocol).

The Protocol-based design keeps Langfuse out of cabal's hard deps: a
prod deployment wires a `LangfuseTraceClient` adapter; tests pass a
recording stub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

from cabal.core.hooks.registry import HookName, HookRegistry

log = structlog.get_logger(__name__)


@runtime_checkable
class TraceClient(Protocol):
    """Minimal interface a backend must implement.

    All methods are sync — the exporter calls them inside async handlers
    that the hook registry will already have moved off the request hot
    path when configured as mode="async". Backends doing blocking I/O
    should batch internally.
    """

    def start_trace(self, *, trace_id: str, task_id: str, target: str, caller: str | None) -> None: ...
    def end_trace(self, *, trace_id: str, task_id: str, state: str, reason_code: int | None) -> None: ...
    def log_generation(
        self,
        *,
        trace_id: str | None,
        span_id: str | None,
        agent: str | None,
        model: str,
        messages: list[dict[str, Any]],
        outcome: str,
        response: Any | None = None,
        error_type: str | None = None,
    ) -> None: ...


@dataclass
class TraceExporter:
    """Wire a TraceClient up to the lifecycle hook registry."""

    client: TraceClient
    mode: str = "async"  # default fire-and-forget so traces never block dispatch

    def register(self, registry: HookRegistry) -> None:
        registry.register(
            HookName.SESSION_START,
            self._on_session_start,
            mode=self.mode,
            label="trace.session_start",
        )
        registry.register(
            HookName.SESSION_END,
            self._on_session_end,
            mode=self.mode,
            label="trace.session_end",
        )
        registry.register(
            HookName.LITELLM_POST,
            self._on_litellm_post,
            mode=self.mode,
            label="trace.litellm_post",
        )

    def _on_session_start(self, payload: dict[str, Any]) -> None:
        trace_id = payload.get("trace_id")
        if trace_id is None:
            return
        try:
            self.client.start_trace(
                trace_id=trace_id,
                task_id=payload["task_id"],
                target=payload["target"],
                caller=payload.get("caller"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace.start_failed", error=str(exc))

    def _on_session_end(self, payload: dict[str, Any]) -> None:
        try:
            self.client.end_trace(
                trace_id=payload.get("trace_id", ""),
                task_id=payload["task_id"],
                state=payload["state"],
                reason_code=payload.get("reason_code"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace.end_failed", error=str(exc))

    def _on_litellm_post(self, payload: dict[str, Any]) -> None:
        try:
            self.client.log_generation(
                trace_id=payload.get("trace_id"),
                span_id=payload.get("span_id"),
                agent=payload.get("agent"),
                model=payload["model"],
                messages=payload.get("messages", []),
                outcome=payload.get("outcome", "success"),
                response=payload.get("response"),
                error_type=payload.get("error_type"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace.generation_failed", error=str(exc))


@dataclass
class RecordingTraceClient:
    """Test/debug TraceClient that just appends to in-memory lists."""

    starts: list[dict[str, Any]] = field(default_factory=list)
    ends: list[dict[str, Any]] = field(default_factory=list)
    generations: list[dict[str, Any]] = field(default_factory=list)

    def start_trace(self, **kwargs: Any) -> None:
        self.starts.append(kwargs)

    def end_trace(self, **kwargs: Any) -> None:
        self.ends.append(kwargs)

    def log_generation(self, **kwargs: Any) -> None:
        self.generations.append(kwargs)
