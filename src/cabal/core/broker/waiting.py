"""The "waiting" outcome: an agent stops until a condition holds (cabal issue 406, part 2).

A worker that reaches a step it may not run yet (a build before its build window)
commits, ends its turn with reason code 705 and

    metadata.cabal_waiting = {"condition": "<what it waits for>", "until": "<ISO 8601>"?}

and stops, instead of sleeping on the clock.

- With `until`, cabal continues it itself: a new task in the same contextId, from
  the same caller, scheduled for `until` (core/scheduler.py), carrying the
  conversation so far. The waiting task reads `completed` with 705 and
  `cabal_continued_by`, so the caller has nothing to do.
- Without `until`, or when cabal can't continue it, the task reads
  `input-required` with 705, and the dispatcher re-dispatches when the condition
  holds. cabal can't continue a task when it doesn't hold the caller's own claims
  (a dispatch through a door that doesn't pass them), when `until` is after the
  caller's token expires, or after `cabal.max_wait_continuations` continuations
  in a row. The reason is in `cabal_reason_message`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from cabal.core.a2a.codes import CODE_WAITING
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart

if TYPE_CHECKING:
    from cabal.core.broker.router import DispatchRouter, _Started

log = structlog.get_logger(__name__)

#: Longest condition text kept on the continuation's message.
CONDITION_MAX = 500


def _paused(response: Task, why: str | None) -> Task:
    metadata = dict(response.metadata)
    if why:
        metadata["cabal_reason_message"] = why
    status = response.status.model_copy(update={"state": "input-required"})
    return response.model_copy(update={"status": status, "metadata": metadata})


def _waiting(response: Task) -> dict[str, Any] | None:
    try:
        code = int(response.metadata.get("cabal_reason_code"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if code != CODE_WAITING:
        return None
    waiting = response.metadata.get("cabal_waiting")
    return waiting if isinstance(waiting, dict) else {}


async def handle_waiting(
    router: DispatchRouter, started: _Started, task: Task, response: Task, *, max_continuations: int
) -> Task:
    """`response` as it is stored: unchanged unless it is a 705 reply."""
    from cabal.core.scheduler import parse_not_before

    waiting = _waiting(response)
    if waiting is None:
        return response
    condition = str(waiting.get("condition") or "the condition you named")[:CONDITION_MAX]
    count = int(task.metadata.get("cabal_wait_count") or 0)
    raw_until = waiting.get("until")
    if raw_until is None:
        return _paused(response, None)
    try:
        until = parse_not_before(raw_until)
    except ValueError as exc:
        return _paused(response, f"cabal can't continue this task: {exc}")
    claims = started.caller_claims
    if not claims:
        return _paused(response, "cabal can't continue this task: it doesn't hold the caller's claims")
    if count >= max_continuations:
        return _paused(
            response,
            f"cabal continued this task {count} times already (cabal.max_wait_continuations); "
            "the caller decides now",
        )
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and until.timestamp() >= exp:
        return _paused(response, "cabal can't continue this task: `until` is after the caller's token expires")

    now = datetime.now(UTC)
    history = [*response.history, *([response.status.message] if response.status.message else [])]
    continuation = Task(
        id=str(uuid.uuid4()),
        contextId=response.context_id or task.context_id,
        status=TaskStatus(
            state="submitted",
            message=Message(
                role="user",
                parts=[
                    TextPart(
                        text=(
                            f"It is now {max(until, now).isoformat()}. You stopped to wait for: "
                            f"{condition}. If that holds, continue where you left off; if it "
                            "doesn't, say so and stop."
                        )
                    )
                ],
            ),
        ),
        history=history,
        metadata={
            "cabal_continues": task.id,
            "cabal_wait_count": count + 1,
            "cabal_not_before": until.isoformat(),
        },
    )
    try:
        # Not router.schedule: its _prepare would re-verify the run's dispatch
        # token, which lasts 5 minutes and is long gone after an hours-long
        # turn. The caller was verified when this run started, and the
        # continuation's start re-checks everything from the stored claims.
        await router._bookkeeper.on_dispatch_scheduled(
            continuation, started.ctx.target, started.ctx.caller_agent, not_before=until, claims=claims
        )
    except Exception as exc:  # noqa: BLE001 -- the reply still has to be stored
        log.warning("waiting.continue_failed", task_id=task.id, error=repr(exc))
        return _paused(response, f"cabal couldn't schedule the continuation: {exc}")
    log.info("waiting.continued", task_id=task.id, continuation=continuation.id, until=until.isoformat())
    metadata = {**response.metadata, "cabal_continued_by": continuation.id}
    status = response.status.model_copy(update={"state": "completed"})
    return response.model_copy(update={"status": status, "metadata": metadata})
