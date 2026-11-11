"""Detached dispatches: the app-owned registry for A2A v1.0 returnImmediately (#277).

A detached task's run phase (router guardrails, the persona call, its
bookkeeping) runs after the SendMessage response has been sent. This registry
owns those runs for their whole life:

- it holds a strong reference to each asyncio task until it ends, so none is
  garbage-collected mid-run;
- it caps how many run at once (`cabal.detached_task_limit`); a caller
  reserves a slot before starting a dispatch, so two concurrent requests
  cannot both slip past the cap;
- on shutdown it asks every run to stop through the AbortCoordinator and
  waits a bounded grace period (`cabal.detached_drain_seconds`). A run that
  has not ended by then is cancelled, and the startup sweep
  (`Bookkeeper.fail_interrupted`) marks its row failed on the next start.

Declared background work, in the shape cabal issue 260 asks for:
    what: one asyncio task per A2A v1.0 SendMessage with returnImmediately
    started by: the SendMessage request
    reason: the client polls GetTask instead of holding the request open
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import structlog

from cabal.core.abort import AbortCoordinator
from cabal.core.running import RunningTask, RunningTasks

log = structlog.get_logger(__name__)


class DetachedTasks:
    def __init__(
        self, *, limit: int, abort: AbortCoordinator, running: RunningTasks | None = None
    ) -> None:
        self._limit = limit
        self._abort = abort
        # The router's running list (issue 405). The run phase removes its own
        # task; this covers a run cancelled before it ever started.
        self._tasks_running = running
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._reserved = 0

    def __len__(self) -> int:
        return len(self._running)

    def try_reserve(self) -> bool:
        """Claim a slot for a dispatch about to start; False at the cap."""
        if len(self._running) + self._reserved >= self._limit:
            return False
        self._reserved += 1
        return True

    def release(self) -> None:
        """Give back a reserved slot that did not become a run."""
        self._reserved = max(0, self._reserved - 1)

    def spawn(self, task_id: str, run: Coroutine[Any, Any, Any]) -> None:
        """Run `run` detached, using a reserved slot."""
        self.release()
        # The running-list entry this run's start phase added. _finished removes
        # only that one: by the time the done-callback runs, the run's own finally
        # has unlisted it, and a resume with the same id may be listed (PR 407 r4).
        entry = self._tasks_running.get(task_id) if self._tasks_running is not None else None
        job = asyncio.get_running_loop().create_task(run, name=f"detached:{task_id}")
        self._running[task_id] = job
        job.add_done_callback(lambda done: self._finished(task_id, done, entry))
        log.info("detached.started", task_id=task_id, running=len(self._running))

    def _finished(self, task_id: str, job: asyncio.Task[Any], entry: RunningTask | None = None) -> None:
        self._running.pop(task_id, None)
        if self._tasks_running is not None and entry is not None:
            self._tasks_running.remove(task_id, entry)
        if job.cancelled():
            log.warning("detached.cancelled", task_id=task_id)
        elif (exc := job.exception()) is not None:
            # A failure in the persona call is already recorded on the task's
            # row by the run phase. One raised after it, in the success
            # bookkeeping, is not; that row is left to the startup sweep.
            log.warning("detached.failed", task_id=task_id, error=repr(exc))

    async def drain(self, grace_seconds: float) -> None:
        """Ask every run to stop, wait up to `grace_seconds`, then cancel the
        rest. Their rows are left for the startup sweep."""
        if not self._running:
            return
        jobs = dict(self._running)
        for task_id in jobs:
            self._abort.abort(task_id, reason="cabal is shutting down")
        _, pending = await asyncio.wait(jobs.values(), timeout=grace_seconds)
        for job in pending:
            job.cancel()
        # Let each cancellation finish unwinding before the app closes the
        # database underneath it.
        await asyncio.gather(*pending, return_exceptions=True)
        log.info("detached.drained", stopped=len(jobs) - len(pending), cancelled=len(pending))
