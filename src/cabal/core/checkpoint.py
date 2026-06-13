"""CheckpointStore Protocol + default SQLite implementation.

cabal#2 MVP cut — single Protocol with three methods (save, load, drop),
default SQLite-backed impl. Future backends (Postgres, Redis, in-memory)
plug in by satisfying the Protocol; persona code does not change. Same
Protocol-over-impl shape as `SecretsBackend` and `LLMClient`.

What gets checkpointed
----------------------

A checkpoint captures the state needed to resume an LLM tool-use loop
between a tool-execution round and the next LLM call. Specifically:

  - `task_id`: identifies the in-flight A2A Task.
  - `step_index`: 0 = before the initial LLM call; N = after the Nth
    tool-use round has been executed and the next LLM call is pending.
  - `messages`: the full chat message history (system, user, assistant
    tool_calls, tool results). On resume, the LLM is called with these
    messages and the tool-use loop continues from there.
  - `persona_state`: per-persona extra (session_id, anything else
    needed to keep audit/session bookkeeping coherent on resume).

What's deferred (follow-ups)
----------------------------

  - `RookPrimusPersona` multi-stage pipeline checkpointing
    (PLAN→IMPLEMENT→REVIEW→AGGREGATE has its own state shape).
  - Resume-on-different-generation semantics — pairs with §6.10
    generation rotation, not yet wired.
  - Streaming resume (`astream_task` does not checkpoint).
  - In-flight HTTP retry — by design per task non-goals. Tool calls
    that were mid-network when the crash happened are re-issued on
    resume; idempotent tool design is the caller's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import open_session

__all__ = [
    "Checkpoint",
    "CheckpointStore",
    "NoOpCheckpointStore",
    "SqliteCheckpointStore",
]


@dataclass(frozen=True)
class Checkpoint:
    """Immutable snapshot of in-flight tool-use loop state.

    `step_index` advances monotonically across the loop:
      - 0 → about to make the initial LLM call.
      - N → just finished tool-use round N, about to make the next call.

    `task_json` is the serialized original Task (model_dump from the A2A
    type). Resume needs it to reconstruct the Task object handed back to
    the handler — specifically for history append in the final response
    and for task.metadata (trace_id, span_id, etc.).
    """

    task_id: str
    step_index: int
    messages: list[dict[str, Any]]
    task_json: dict[str, Any] = field(default_factory=dict)
    persona_state: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@runtime_checkable
class CheckpointStore(Protocol):
    """Pluggable storage for in-flight tool-use loop checkpoints."""

    async def save(self, checkpoint: Checkpoint) -> None:
        """Persist `checkpoint`. Upserts on `task_id` — latest write wins
        because resume only ever needs the most recent step."""
        ...

    async def load(self, task_id: str) -> Checkpoint | None:
        """Return the latest checkpoint for `task_id`, or None."""
        ...

    async def drop(self, task_id: str) -> bool:
        """Delete the checkpoint for `task_id`. Returns True if a row
        existed, False if no-op. Called once the Task completes —
        successful flows shouldn't leave checkpoint debris around."""
        ...


class NoOpCheckpointStore:
    """Default no-op CheckpointStore — wired into LLMPersona when the
    persona has not opted into checkpointing. Avoids `None` checks on
    every save/load call in the hot path."""

    async def save(self, checkpoint: Checkpoint) -> None:  # noqa: D401, ARG002
        return None

    async def load(self, task_id: str) -> Checkpoint | None:  # noqa: ARG002
        return None

    async def drop(self, task_id: str) -> bool:  # noqa: ARG002
        return False


@dataclass(slots=True)
class SqliteCheckpointStore:
    """Default CheckpointStore — writes to the same SQLite DB as the rest
    of cabal core. New backends (Postgres for multi-node, Redis for
    high-frequency writes, in-memory for tests) implement the Protocol
    directly.
    """

    sessionmaker: async_sessionmaker[AsyncSession]

    async def save(self, checkpoint: Checkpoint) -> None:
        # Import here so the migration's table definition is loaded
        # before we touch the ORM (avoids circular import at module load).
        from cabal.core.models import TaskCheckpoint

        async with open_session(self.sessionmaker) as session:
            stmt = sqlite_insert(TaskCheckpoint).values(
                task_id=checkpoint.task_id,
                step_index=checkpoint.step_index,
                messages_json=checkpoint.messages,
                task_json=checkpoint.task_json,
                persona_state_json=checkpoint.persona_state,
                updated_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[TaskCheckpoint.task_id],
                set_={
                    "step_index": stmt.excluded.step_index,
                    "messages_json": stmt.excluded.messages_json,
                    "task_json": stmt.excluded.task_json,
                    "persona_state_json": stmt.excluded.persona_state_json,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)

    async def load(self, task_id: str) -> Checkpoint | None:
        from cabal.core.models import TaskCheckpoint

        async with open_session(self.sessionmaker) as session:
            result = await session.execute(
                select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id)
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return Checkpoint(
            task_id=row.task_id,
            step_index=row.step_index,
            messages=row.messages_json,
            task_json=row.task_json or {},
            persona_state=row.persona_state_json or {},
            created_at=row.created_at,
        )

    async def drop(self, task_id: str) -> bool:
        from cabal.core.models import TaskCheckpoint

        async with open_session(self.sessionmaker) as session:
            result = await session.execute(
                delete(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id)
            )
        return (result.rowcount or 0) > 0
