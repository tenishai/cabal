"""Periodic heartbeat for locally-registered agents.

In solo mode every persona runs in-process; the heartbeat scheduler
updates `agents.last_heartbeat` so the bulletin board reflects liveness
and the operator can spot a stuck handler quickly.

In scale mode each container runs its own scheduler against its own
agent row. Same code path — only what's in the LocalHandler registry
gets pinged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Agent

log = structlog.get_logger(__name__)


class HeartbeatScheduler:
    """Background task that pings active-generation rows for given names."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        interval_seconds: float = 30.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._local_names: set[str] = set()

    def watch(self, names: Iterable[str]) -> None:
        """Add names whose active generation should be pinged."""
        self._local_names.update(names)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="cabal-heartbeat")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        # One failed ping (a locked DB, a dropped connection) must not end the
        # scheduler: it is caught per iteration, logged, and the loop carries
        # on at the next interval (cabal#230).
        # Initial ping right at startup so list_agents reflects liveness
        # immediately.
        await self._ping_guarded()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                await self._ping_guarded()

    async def _ping_guarded(self) -> None:
        try:
            await self._ping_once()
        except Exception as exc:  # noqa: BLE001
            log.error("heartbeat.ping_failed", error=str(exc))

    async def _ping_once(self) -> None:
        async with open_session(self._sessionmaker) as session:
            result = await session.execute(
                select(Agent.name, Agent.generation).where(
                    Agent.name.in_(self._local_names),
                    Agent.status == "active",
                )
            )
            rows = list(result.all())
            for name, generation in rows:
                await bulletin_store.heartbeat(session, name, generation)
        if rows:
            log.debug("heartbeat.pinged", count=len(rows))
