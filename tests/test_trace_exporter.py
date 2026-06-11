"""TraceExporter wires SESSION_START/END + LITELLM_POST → TraceClient."""

from __future__ import annotations

import asyncio

import pytest

from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.observability.trace_exporter import (
    RecordingTraceClient,
    TraceExporter,
)


@pytest.mark.asyncio
async def test_session_start_recorded() -> None:
    client = RecordingTraceClient()
    reg = HookRegistry()
    TraceExporter(client=client, mode="sync").register(reg)

    await reg.fire(
        HookName.SESSION_START,
        {
            "task_id": "t-1",
            "target": "archivist",
            "caller": "operator:dimartin",
            "trace_id": "a" * 32,
        },
    )
    assert len(client.starts) == 1
    assert client.starts[0]["trace_id"] == "a" * 32
    assert client.starts[0]["target"] == "archivist"


@pytest.mark.asyncio
async def test_session_start_without_trace_id_is_ignored() -> None:
    client = RecordingTraceClient()
    reg = HookRegistry()
    TraceExporter(client=client, mode="sync").register(reg)
    await reg.fire(HookName.SESSION_START, {"task_id": "t", "target": "x"})
    assert client.starts == []


@pytest.mark.asyncio
async def test_session_end_recorded_with_reason_code() -> None:
    client = RecordingTraceClient()
    reg = HookRegistry()
    TraceExporter(client=client, mode="sync").register(reg)
    await reg.fire(
        HookName.SESSION_END,
        {
            "task_id": "t-1",
            "trace_id": "a" * 32,
            "state": "failed",
            "reason_code": 702,
        },
    )
    assert len(client.ends) == 1
    assert client.ends[0]["reason_code"] == 702
    assert client.ends[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_litellm_post_recorded_with_input_output() -> None:
    client = RecordingTraceClient()
    reg = HookRegistry()
    TraceExporter(client=client, mode="sync").register(reg)
    await reg.fire(
        HookName.LITELLM_POST,
        {
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "agent": "archivist",
            "model": "anthropic/claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "outcome": "success",
            "response": {"choices": [{"message": {"content": "ok"}}]},
        },
    )
    assert len(client.generations) == 1
    gen = client.generations[0]
    assert gen["model"] == "anthropic/claude-sonnet-4-6"
    assert gen["agent"] == "archivist"
    assert gen["outcome"] == "success"


@pytest.mark.asyncio
async def test_async_mode_does_not_block() -> None:
    """Default mode='async' fires the handler in a task, returns immediately."""
    client = RecordingTraceClient()
    reg = HookRegistry()
    TraceExporter(client=client, mode="async").register(reg)
    await reg.fire(
        HookName.SESSION_START,
        {"task_id": "t", "target": "x", "trace_id": "a" * 32},
    )
    # Recording may or may not have completed yet; give it a moment
    await asyncio.sleep(0.05)
    assert len(client.starts) == 1


@pytest.mark.asyncio
async def test_client_exception_does_not_break_dispatch() -> None:
    """A failing trace client must not raise into the dispatch flow."""

    class Boom:
        def start_trace(self, **kwargs):
            raise RuntimeError("trace backend offline")

        def end_trace(self, **kwargs):
            raise RuntimeError("still offline")

        def log_generation(self, **kwargs):
            raise RuntimeError("nope")

    reg = HookRegistry()
    TraceExporter(client=Boom(), mode="sync").register(reg)
    # Must not raise
    await reg.fire(
        HookName.SESSION_START,
        {"task_id": "t", "target": "x", "trace_id": "a" * 32},
    )
    await reg.fire(
        HookName.SESSION_END,
        {"task_id": "t", "trace_id": "a" * 32, "state": "completed", "reason_code": None},
    )
