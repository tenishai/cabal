"""Running tasks and the drain (cabal issue 405).

`RunningTasks` is the in-process list of dispatches that are running now. The
router adds a task as soon as it is admitted, before its conversations row is
written, and removes it only after the result is written, on every path: sync,
detached, streaming and a local resume. An id that is already listed is refused. A `conversations` row alone can't say this: a row stays
`working` if the bookkeeping after the persona call raises, and the startup
sweep only clears rows from an earlier process.

The drain is a step an operator (or `ergon down --drain`) takes before
stopping cabal. While draining, a new top-level dispatch is refused with
`DrainingError`. A dispatch from an agent that is itself the target of a
running task is let through, because it is part of the work the drain is
waiting for (a coder asking a reviewer, a persona fanning out). That check
uses the caller's verified token subject, never task metadata such as
`cabal_parent_task`. Inbound routes strip `cabal_*` keys since PR 402, and
the rule must not depend on that. A passed
deadline is only reported: cabal aborts nothing, and the caller decides
whether to stop anyway. Drain state is in memory, so a restart ends it.

Shutdown on SIGTERM is unchanged (`DetachedTasks.drain`): the fast stop.

Declared state, in the shape cabal issue 260 asks for:
    what: a dict of running task ids, and the drain flag and deadline
    written by: the dispatch router (tasks), the admin drain routes (drain)
    reason: an operator needs a real "is anything busy" check before a restart
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunningTask:
    task_id: str
    target_agent: str
    caller_agent: str | None
    started_at: datetime
    detached: bool = False
    streaming: bool = False
    parent_task: str | None = None


@dataclass
class _Drain:
    started_at: datetime
    deadline_mono: float | None
    deadline_at: datetime | None


def _subject_agents(sub: str | None) -> set[str]:
    """The agent names a token subject can stand for. ergon's colleague passes
    use `agent:<name>`, persona tokens use the bare name; the router's
    `target_agent` is the bare name."""
    if not sub:
        return set()
    names = {sub}
    if sub.startswith("agent:"):
        names.add(sub[len("agent:") :])
    return names


@dataclass
class RunningTasks:
    _tasks: dict[str, RunningTask] = field(default_factory=dict)
    _drain: _Drain | None = None

    # -- tasks -----------------------------------------------------------

    def add(self, task: RunningTask) -> None:
        self._tasks[task.task_id] = task
        log.debug("running.add", task_id=task.task_id, target=task.target_agent, running=len(self._tasks))

    def remove(self, task_id: str) -> None:
        """Idempotent: the run's own `finally` and the detached registry's
        done-callback may both call it."""
        if self._tasks.pop(task_id, None) is not None:
            log.debug("running.remove", task_id=task_id, running=len(self._tasks))

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    def list(self) -> list[RunningTask]:
        """Oldest first."""
        return sorted(self._tasks.values(), key=lambda t: t.started_at)

    def busy_agents(self) -> set[str]:
        return {t.target_agent for t in self._tasks.values()}

    # -- drain -----------------------------------------------------------

    @property
    def draining(self) -> bool:
        return self._drain is not None

    def start_drain(self, deadline_s: float | None) -> None:
        """Begin draining, or move the deadline of a drain already begun."""
        now = datetime.now(UTC)
        if deadline_s is None:
            mono, at = None, None
        else:
            mono = time.monotonic() + deadline_s
            at = datetime.fromtimestamp(now.timestamp() + deadline_s, UTC)
        started = self._drain.started_at if self._drain is not None else now
        self._drain = _Drain(started_at=started, deadline_mono=mono, deadline_at=at)
        log.warning("drain.started", deadline_s=deadline_s, running=len(self._tasks))

    def cancel_drain(self) -> bool:
        """Accept dispatches again. False if cabal was not draining."""
        was = self._drain is not None
        self._drain = None
        if was:
            log.warning("drain.cancelled", running=len(self._tasks))
        return was

    def deadline_passed(self) -> bool:
        d = self._drain
        return d is not None and d.deadline_mono is not None and time.monotonic() >= d.deadline_mono

    def admits(self, caller_sub: str | None) -> bool:
        """Whether a new dispatch from `caller_sub` may start now."""
        if self._drain is None:
            return True
        return bool(_subject_agents(caller_sub) & self.busy_agents())

    def drain_state(self) -> dict[str, object]:
        d = self._drain
        return {
            "draining": d is not None,
            "drain_started_at": d.started_at.isoformat() if d else None,
            "drain_deadline": d.deadline_at.isoformat() if d and d.deadline_at else None,
            "deadline_passed": self.deadline_passed(),
        }
