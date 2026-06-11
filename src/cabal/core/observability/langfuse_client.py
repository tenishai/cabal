"""Langfuse adapter for TraceClient.

Lazily imports `langfuse` so the dependency stays optional. Tests use
RecordingTraceClient from trace_exporter; production wires this when
identity.toml provides a [langfuse] block.

Config (identity.toml):
    [langfuse]
    host = "https://cloud.langfuse.com"
    public_key_env = "LANGFUSE_PUBLIC_KEY"
    secret_key_env = "LANGFUSE_SECRET_KEY"
"""

from __future__ import annotations

import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class LangfuseTraceClient:
    """Wrap the langfuse SDK to satisfy TraceClient.

    Constructed via `from_env(host, public_key_env, secret_key_env)` so
    secrets stay in env and the SDK import stays lazy.
    """

    def __init__(self, client: Any) -> None:  # langfuse.Langfuse
        self._client = client
        self._traces: dict[str, Any] = {}

    @classmethod
    def from_env(
        cls, *, host: str | None, public_key_env: str | None, secret_key_env: str | None
    ) -> "LangfuseTraceClient | None":
        public_key = os.environ.get(public_key_env) if public_key_env else None
        secret_key = os.environ.get(secret_key_env) if secret_key_env else None
        if not (public_key and secret_key and host):
            log.info(
                "langfuse.config_incomplete",
                msg="langfuse host/keys not set; trace export disabled",
            )
            return None

        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]
        except ImportError:
            log.warning(
                "langfuse.missing_dependency",
                msg="pip install langfuse to enable tracing",
            )
            return None

        client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)
        return cls(client)

    def start_trace(
        self, *, trace_id: str, task_id: str, target: str, caller: str | None
    ) -> None:
        trace = self._client.trace(
            id=trace_id,
            name=f"cabal:{target}",
            metadata={"task_id": task_id, "caller": caller},
        )
        self._traces[trace_id] = trace

    def end_trace(
        self,
        *,
        trace_id: str,
        task_id: str,
        state: str,
        reason_code: int | None,
    ) -> None:
        trace = self._traces.pop(trace_id, None)
        if trace is None:
            return
        trace.update(metadata={"final_state": state, "reason_code": reason_code})

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
    ) -> None:
        trace = self._traces.get(trace_id) if trace_id else None
        target = trace if trace is not None else self._client
        target.generation(
            name=f"cabal:llm:{agent or 'unknown'}",
            model=model,
            input=messages,
            output=response,
            metadata={
                "outcome": outcome,
                "error_type": error_type,
                "span_id": span_id,
            },
        )
