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
            existing = await session.get(Conversation, task.id)
            if (
                existing is not None
                and existing.schedule_json is not None
                and existing.state == "submitted"
            ):
                # A scheduled task the Scheduler just claimed (cabal issue
                # 406): its row exists, so this start updates it. Any other
                # existing row still collides, as before.
                existing.target_generation = target.generation
                existing.caller_agent = caller_agent
                existing.state = task.status.state
                existing.updated_at = datetime.now(UTC)
            else:
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
                # The task's own metadata rides every published event so
                # consumers (a room's SSE stream) can scope frames without a
                # second lookup: kobo drops thread-marked frames from viewers
                # who are not the thread's initiator.
                "metadata": task.metadata,
            },
        )

    async def on_dispatch_scheduled(
        self,
        task: Task,
        target: AgentRow,
        caller_agent: str | None,
        *,
        not_before: datetime,
        claims: dict,
    ) -> None:
        """A dispatch that starts at `not_before` (cabal issue 406): its row,
        in state `scheduled`, with the submitted Task (for GetTask and the
        start) and the caller's claims. No token is stored."""
        async with open_session(self._sessionmaker) as session:
            session.add(
                Conversation(
                    task_id=task.id,
                    target_agent=target.name,
                    target_generation=target.generation,
                    caller_agent=caller_agent,
                    state="scheduled",
                    result_json=task.model_dump(by_alias=True, exclude_none=True),
                    not_before=not_before,
                    schedule_json=claims,
                )
            )
            session.add(
                Event(
                    task_id=task.id,
                    event_type="dispatch_scheduled",
                    payload_json={
                        "target": target.name,
                        "caller": caller_agent,
                        "not_before": not_before.isoformat(),
                    },
                )
            )
        log.info("dispatch.scheduled", task_id=task.id, target=target.name, not_before=not_before.isoformat())

    async def on_dispatch_working(self, task: Task) -> None:
        """The persona is about to be called: `submitted` becomes `working`,
        so GetTask can tell a started task from a queued one (#277). Only a
        `submitted` row moves, so this never overwrites an end state."""
        from sqlalchemy import update

        async with open_session(self._sessionmaker) as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.task_id == task.id, Conversation.state == "submitted")
                .values(state="working", updated_at=datetime.now(UTC))
            )

    async def fail_interrupted(self) -> int:
        """Mark every task still `submitted` or `working` as failed, with the
        status message "interrupted by a restart". For startup, before
        serving: nothing in a fresh process is running, so such a row was cut
        off by a stop or a crash (#277). Assumes one cabal process per
        database. Returns the number of tasks marked."""
        from sqlalchemy import select, update

        from cabal.core.a2a.types import Message, TaskStatus, TextPart

        async with open_session(self._sessionmaker) as session:
            rows = (
                await session.execute(
                    select(Conversation).where(Conversation.state.in_(("submitted", "working")))
                )
            ).scalars().all()
            for row in rows:
                # A2A has no `interrupted` state, so the task is `failed`;
                # `cabal_interrupted` tells a caller cabal went down under the
                # work, not that the work failed (issue 405).
                stopped = Task(
                    id=row.task_id,
                    status=TaskStatus(
                        state="failed",
                        message=Message(
                            role="agent", parts=[TextPart(text="interrupted by a restart")]
                        ),
                    ),
                    metadata={"cabal_interrupted": True},
                )
                await session.execute(
                    update(Conversation)
                    .where(Conversation.task_id == row.task_id)
                    .values(
                        state="failed",
                        updated_at=datetime.now(UTC),
                        result_json=stopped.model_dump(by_alias=True, exclude_none=True),
                    )
                )
                session.add(
                    Event(
                        task_id=row.task_id,
                        event_type="dispatch_interrupted",
                        payload_json={"previous_state": row.state},
                    )
                )
        if rows:
            log.warning("dispatch.interrupted_by_restart", count=len(rows))
        return len(rows)

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
            {"state": response.status.state, "metadata": task.metadata},
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
            {"error_type": type(error).__name__, "error": str(error), "metadata": task.metadata},
        )
