"""Broker bookkeeping: conversation rows + events table writes.

§6.12 invariant 3 — the in-process dispatch path goes THROUGH this module
just like the HTTP path does. That keeps the audit trail uniform across
modes: both write a conversation row at dispatch_start, both append events
on success/failure, both fire lifecycle hooks. The only thing the in-process
path skips is the network layer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.a2a.types import Task
from cabal.core.bulletin.store import AgentRow
from cabal.core.db import open_session
from cabal.core.models import Conversation, Event
from cabal.core.observability.event_bus import EventBus

log = structlog.get_logger(__name__)


class Bookkeeper:
    """Writes conversation rows + events for every dispatch.

    Single instance per Cabal Core process; shared between the in-process
    and HTTP dispatch paths.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        event_bus: EventBus | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._event_bus = event_bus

    def _publish(self, event_type: str, task_id: str | None, payload: dict) -> None:
        if self._event_bus is None:
            return
        self._event_bus.publish(
            {
                "event_type": event_type,
                "task_id": task_id,
                "payload": payload,
            }
        )

    async def on_dispatch_start(
        self,
        task: Task,
        target: AgentRow,
        caller_agent: str | None,
    ) -> None:
        async with open_session(self._sessionmaker) as session:
            session.add(
                Conversation(
                    task_id=task.id,
                    target_agent=target.name,
                    target_generation=target.generation,
                    caller_agent=caller_agent,
                    state=task.status.state,
                    cabal_escalated_from=task.metadata.get("cabal_escalated_from"),
                )
            )
            session.add(
                Event(
                    task_id=task.id,
                    event_type="dispatch_start",
                    payload_json={
                        "target": target.name,
                        "generation": target.generation,
                        "caller": caller_agent,
                        "trace_id": task.metadata.get("cabal_trace_id"),
                        "span_id": task.metadata.get("cabal_span_id"),
                    },
                )
            )
        log.info(
            "dispatch.start",
            task_id=task.id,
            target=target.name,
            generation=target.generation,
        )
        self._publish(
            "dispatch_start",
            task.id,
            {
                "target": target.name,
                "generation": target.generation,
                "caller": caller_agent,
                "trace_id": task.metadata.get("cabal_trace_id"),
            },
        )

    async def on_dispatch_success(self, task: Task, response: Task) -> None:
        from sqlalchemy import update

        async with open_session(self._sessionmaker) as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.task_id == task.id)
                .values(
                    state=response.status.state,
                    updated_at=datetime.now(UTC),
                    result_json=response.model_dump(by_alias=True, exclude_none=True),
                )
            )
            session.add(
                Event(
                    task_id=task.id,
                    event_type="dispatch_success",
                    payload_json={"state": response.status.state},
                )
            )
        log.info("dispatch.success", task_id=task.id, state=response.status.state)
        self._publish(
            "dispatch_success",
            task.id,
            {"state": response.status.state},
        )

    async def on_dispatch_failure(self, task: Task, error: Exception) -> None:
        from sqlalchemy import update

        async with open_session(self._sessionmaker) as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.task_id == task.id)
                .values(state="failed", updated_at=datetime.now(UTC))
            )
            session.add(
                Event(
                    task_id=task.id,
                    event_type="dispatch_failure",
                    payload_json={"error_type": type(error).__name__, "error": str(error)},
                )
            )
        log.warning(
            "dispatch.failure", task_id=task.id, error_type=type(error).__name__
        )
        self._publish(
            "dispatch_failure",
            task.id,
            {"error_type": type(error).__name__, "error": str(error)},
        )
