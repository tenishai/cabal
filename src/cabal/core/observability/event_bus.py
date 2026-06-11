"""In-process pub/sub for events the Bookkeeper writes.

Each event written to the `events` table is also pushed onto every
subscriber's asyncio.Queue. Subscribers are SSE-serving HTTP endpoints
(/admin/events/stream) that drain their queue to the client.

This stays in-memory: subscribers see events emitted while they're
connected. For historical replay, they query the events table directly.
A future Phase could add a Postgres LISTEN/NOTIFY bridge for multi-node
deployments — for now, single-process Cabal Core is the only mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class EventBus:
    """Simple async-fanout bus.

    Subscribers create a Queue via `subscribe()`; the bus pushes every
    `publish()` call to every active subscriber. When a subscriber drops
    its queue (context manager exit), it's removed from the fanout.
    """

    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    queue_maxsize: int = 256

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.queue_maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, event: dict[str, Any]) -> None:
        """Push to all subscribers. Drops events that overflow a queue —
        a slow consumer can't backpressure the producer."""
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "event_bus.subscriber_overflow",
                    msg="dropped event for slow subscriber",
                )

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Convenience async iterator — subscribes, yields, cleans up."""
        q = self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(q)
