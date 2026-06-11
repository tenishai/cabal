"""EventBus pub/sub tests."""

from __future__ import annotations

import asyncio

import pytest

from cabal.core.observability.event_bus import EventBus


@pytest.mark.asyncio
async def test_publish_reaches_all_subscribers() -> None:
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.publish({"event_type": "dispatch_start", "task_id": "t-1"})

    e1 = await asyncio.wait_for(q1.get(), timeout=1)
    e2 = await asyncio.wait_for(q2.get(), timeout=1)
    assert e1["task_id"] == "t-1"
    assert e2["task_id"] == "t-1"


@pytest.mark.asyncio
async def test_unsubscribe_removes_subscriber() -> None:
    bus = EventBus()
    q = bus.subscribe()
    assert bus.subscriber_count() == 1
    bus.unsubscribe(q)
    assert bus.subscriber_count() == 0
    bus.publish({"x": 1})
    assert q.empty()  # nothing pushed after unsubscribe


@pytest.mark.asyncio
async def test_full_queue_drops_events_not_blocks() -> None:
    """A slow consumer can't backpressure the producer."""
    bus = EventBus(queue_maxsize=2)
    q = bus.subscribe()
    bus.publish({"i": 0})
    bus.publish({"i": 1})
    # 3rd publish should drop, not block
    bus.publish({"i": 2})
    assert q.qsize() == 2


@pytest.mark.asyncio
async def test_no_subscribers_publish_is_silent() -> None:
    bus = EventBus()
    # Should not raise
    bus.publish({"event_type": "noop"})


@pytest.mark.asyncio
async def test_stream_helper_yields_and_cleans_up_on_close() -> None:
    """stream() yields published events; explicit aclose() unsubscribes."""
    bus = EventBus()

    async def emit_after_subscribe() -> None:
        await asyncio.sleep(0.02)
        bus.publish({"i": 99})

    emit_task = asyncio.create_task(emit_after_subscribe())
    gen = bus.stream()
    consumed = await asyncio.wait_for(gen.__anext__(), timeout=1)
    await emit_task
    assert consumed == {"i": 99}
    # Generator still open until we close it (production: FastAPI calls
    # aclose() when the SSE response closes)
    assert bus.subscriber_count() == 1
    await gen.aclose()
    assert bus.subscriber_count() == 0
