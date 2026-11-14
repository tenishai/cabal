"""Scheduled dispatches: start a task at its not-before time (cabal issue 406).

A2A v1 SendMessage with returnImmediately and `metadata.cabal_not_before` records
the task as a `conversations` row in state `scheduled` (router.schedule). This
loop starts due rows through the same detached path as any returnImmediately
dispatch, so a scheduled task is listed, drained and cancelled like the rest
once it runs.

- A row is CLAIMED with a conditional update (`scheduled` -> `submitted`), so a
  CancelTask racing the start, or a second tick, gets exactly one outcome.
- While cabal is draining (issue 405) due rows wait: they are new work. At the
  detached cap the rest wait for the next tick.
- The start re-checks the caller from the STORED claims (expiry and revocation
  always; scope, sub and pairs unless the stored `aud` already was the target's,
  as the live door would) and mints a fresh dispatch token
  (`scheduled_dispatch_token`).
  A row that fails a check is marked failed with the reason, once.
- A time missed while cabal was down starts on the first tick after; the task
  carries `cabal_started_late_s`. Rows survive restarts: the startup sweep only
  touches `submitted` and `working`.

Declared background work, in the shape cabal issue 260 asks for:
    what: one asyncio loop, every `cabal.schedule_poll_seconds`
    started by: the app lifespan, after the detached-task registry
    reason: start work later without a caller or a sleeping worker
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.db import open_session
from cabal.core.models import Conversation, Event

log = structlog.get_logger(__name__)

SCHEDULED = "scheduled"


def group_members(app_state: Any, group: str) -> list[str] | None:
    """`[groups]` in identity.toml (cabal issue 423): the group's members in
    order, or None for a group the config doesn't name."""
    identity = getattr(app_state, "identity", None)
    groups: dict[str, list[str]] = identity.groups if identity is not None else {}
    return groups.get(group)


def parse_not_before(value: Any) -> datetime:
    """`cabal_not_before` as an aware UTC datetime. ISO 8601 with an offset;
    a naive time is refused, because the server's zone is not the caller's."""
    if not isinstance(value, str):
        raise ValueError("cabal_not_before must be an ISO 8601 string")
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"cabal_not_before {value!r} is not an ISO 8601 time") from exc
    if when.tzinfo is None:
        raise ValueError(f"cabal_not_before {value!r} has no UTC offset")
    return when.astimezone(UTC)


def _aware(when: datetime) -> datetime:
    """SQLite hands timestamps back naive; they were written as UTC."""
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _ended(task: Task, state: str, text: str) -> dict[str, Any]:
    return task.model_copy(
        update={"status": TaskStatus(state=state, message=Message(role="agent", parts=[TextPart(text=text)]))}  # type: ignore[arg-type]
    ).model_dump(by_alias=True, exclude_none=True)


async def _end_row(
    session: AsyncSession, row: Conversation, *, from_state: str, state: str, text: str, event: str
) -> bool:
    """Move `row` from `from_state` to an end state, only if it is still in
    `from_state`. True if this call did it."""
    stored = Task.model_validate(row.result_json) if row.result_json else Task(
        id=row.task_id, status=TaskStatus(state="submitted")
    )
    result = await session.execute(
        update(Conversation)
        .where(Conversation.task_id == row.task_id, Conversation.state == from_state)
        .values(state=state, updated_at=datetime.now(UTC), result_json=_ended(stored, state, text))
    )
    if result.rowcount != 1:
        return False
    session.add(Event(task_id=row.task_id, event_type=event, payload_json={"reason": text}))
    return True


async def cancel_scheduled(sessionmaker: async_sessionmaker, task_id: str) -> bool:
    """CancelTask on a task that hasn't started. False if it started (or
    ended) in the meantime, so the caller falls back to the running path."""
    async with open_session(sessionmaker) as session:
        row = await session.get(Conversation, task_id)
        if row is None or row.state != SCHEDULED:
            return False
        done = await _end_row(
            session, row, from_state=SCHEDULED, state="canceled",
            text="canceled by the caller before it started", event="dispatch_schedule_canceled",
        )
    if done:
        log.info("schedule.canceled", task_id=task_id)
    return done


async def scheduled_rows(sessionmaker: async_sessionmaker, limit: int = 200) -> list[Conversation]:
    async with open_session(sessionmaker) as session:
        return list(
            (
                await session.execute(
                    select(Conversation)
                    .where(Conversation.state == SCHEDULED)
                    .order_by(Conversation.not_before, Conversation.task_id)
                    .limit(limit)
                )
            ).scalars()
        )


async def count_scheduled(sessionmaker: async_sessionmaker) -> int:
    from sqlalchemy import func

    async with open_session(sessionmaker) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Conversation).where(Conversation.state == SCHEDULED)
                )
            ).scalar_one()
        )


class Scheduler:
    def __init__(self, *, app_state: Any, interval_seconds: float = 15.0) -> None:
        self._state = app_state
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="cabal-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 -- one bad tick must not end the loop
                log.warning("schedule.tick_failed", error=repr(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    async def _claim_next(self, now: datetime, busy: set[str]) -> tuple[Conversation, str | None] | None:
        """The oldest due row it can start, moved to `submitted` by this call,
        with why it must fail instead (or None); or None when there is none.

        A group row (cabal issue 423) starts on its group's first member that
        is registered and has nothing running (`busy`); the claim moves the
        row's target to that member. While every member is busy it is
        skipped, and later rows can still start."""
        from cabal.core.bulletin import store as bulletin_store

        async with open_session(self._state.sessionmaker) as session:
            candidates = (
                await session.execute(
                    select(Conversation)
                    .where(Conversation.state == SCHEDULED, Conversation.not_before <= now)
                    .order_by(Conversation.not_before, Conversation.task_id)
                    .limit(200)
                )
            ).scalars().all()
            for row in candidates:
                values: dict[str, Any] = {"state": "submitted", "updated_at": datetime.now(UTC)}
                problem: str | None = None
                if row.target_group is not None:
                    members = group_members(self._state, row.target_group)
                    if not members:
                        problem = f"no group {row.target_group!r} in [groups]"
                    else:
                        member = None
                        for name in members:
                            if name in busy:
                                continue
                            agent = await bulletin_store.get_active(session, name)
                            if agent is not None:
                                member = agent
                                break
                        if member is None:
                            log.debug("schedule.group_busy", task_id=row.task_id, group=row.target_group)
                            continue
                        values |= {"target_agent": member.name, "target_generation": member.generation}
                claimed = await session.execute(
                    update(Conversation)
                    .where(Conversation.task_id == row.task_id, Conversation.state == SCHEDULED)
                    .values(**values)
                )
                if claimed.rowcount == 1:
                    # The ORM update syncs `row` (synchronize_session), so a group
                    # row now carries the member it starts on.
                    return row, problem
        return None

    async def _fail(self, row: Conversation, why: str) -> None:
        async with open_session(self._state.sessionmaker) as session:
            await _end_row(
                session, row, from_state="submitted", state="failed",
                text=f"the scheduled task could not start: {why}", event="dispatch_schedule_failed",
            )
        log.warning("schedule.start_failed", task_id=row.task_id, reason=why)

    async def _unclaim(self, row: Conversation) -> None:
        async with open_session(self._state.sessionmaker) as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.task_id == row.task_id, Conversation.state == "submitted")
                .values(state=SCHEDULED, updated_at=datetime.now(UTC))
            )

    async def tick(self, now: datetime | None = None) -> int:
        """Start the due rows there is room for. Returns how many started."""
        from cabal.core.broker.router import DispatchError, DrainingError
        from cabal.core.routes._dispatch_auth import DispatchRefusal, scheduled_dispatch_token

        router = self._state.dispatch_router
        detached = self._state.detached
        if router.running.draining:
            return 0
        started = 0
        while True:
            current = now or datetime.now(UTC)
            if not detached.try_reserve():
                break
            claimed = await self._claim_next(current, router.running.busy_agents())
            if claimed is None:
                detached.release()
                break
            row, problem = claimed
            if problem is not None:
                detached.release()
                await self._fail(row, problem)
                continue
            token = await scheduled_dispatch_token(self._state, row.target_agent, row.schedule_json or {})
            if isinstance(token, DispatchRefusal):
                detached.release()
                await self._fail(row, token.message)
                continue
            task = Task.model_validate(row.result_json)
            assert row.not_before is not None
            late = (current - _aware(row.not_before)).total_seconds()
            task.metadata["cabal_started_late_s"] = round(max(0.0, late), 1)
            try:
                started_task, run = await router.start_detached(
                    target_agent=row.target_agent, target_generation=None, task=task,
                    caller_jwt=token, scheduled=True, caller_claims=row.schedule_json,
                )
            except DrainingError:
                # A drain began between the check above and this start.
                detached.release()
                await self._unclaim(row)
                break
            except DispatchError as exc:
                detached.release()
                await self._fail(row, str(exc))
                continue
            except BaseException:
                detached.release()
                raise
            detached.spawn(started_task.id, run)
            started += 1
            log.info(
                "schedule.started", task_id=row.task_id, target=row.target_agent,
                group=row.target_group, late_s=round(late, 1),
            )
        return started
