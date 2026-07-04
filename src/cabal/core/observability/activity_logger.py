"""ActivityLogger — durable per-activity rows in the events table.

Registers hook handlers for TOOL_USE and LITELLM_POST and writes rows
to the existing ``events`` table (models.Event). Unlike checkpoints,
these rows are NEVER dropped on task success — that's the whole point.

Design decisions
----------------
* **Two rows per tool call** (``tool_call`` + ``tool_result``) rather than
  one combined row.  The split lets future consumers query "which calls
  never produced a result" (crash-during-tool-execution gap) and clearly
  separates the intent (arguments) from the outcome (result, latency,
  error flag).  Both rows are written atomically in a single session
  commit from the single TOOL_USE event, so there is no partial-write risk.

* **Sync mode** — the handlers write to SQLite which is already async, but
  the await is cheap and we want durability before the dispatch loop
  continues.  If write latency becomes a concern the mode can be flipped to
  ``"async"`` without changing the handler logic.

* **Tokens / cost** are explicitly absent — that is scope #B4 / issue #51.
  Do not add them here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import open_session
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.models import Event

log = structlog.get_logger(__name__)


@dataclass
class ActivityLogger:
    """Wire the TOOL_USE and LITELLM_POST hooks to durable event rows.

    Pass ``sessionmaker`` — the same one wired to the rest of the app —
    so writes land in the same SQLite file as all other events.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    mode: str = "sync"

    def register(self, registry: HookRegistry) -> None:
        registry.register(
            HookName.TOOL_USE,
            self._on_tool_use,
            mode=self.mode,
            label="activity.tool_use",
        )
        registry.register(
            HookName.LITELLM_POST,
            self._on_litellm_post,
            mode=self.mode,
            label="activity.litellm_post",
        )

    async def _on_tool_use(self, payload: dict[str, Any]) -> None:
        """Write a ``tool_call`` row and a ``tool_result`` row."""
        task_id = payload.get("task_id")
        tool_name = payload.get("tool_name", "")
        try:
            async with open_session(self.sessionmaker) as session:
                session.add(
                    Event(
                        task_id=task_id,
                        event_type="tool_call",
                        payload_json={
                            "tool_name": tool_name,
                            "arguments": payload.get("arguments"),
                            "tool_call_id": payload.get("tool_call_id"),
                        },
                    )
                )
                session.add(
                    Event(
                        task_id=task_id,
                        event_type="tool_result",
                        payload_json={
                            "tool_name": tool_name,
                            "result_summary": payload.get("result_summary"),
                            "is_error": payload.get("is_error", False),
                            "latency_ms": payload.get("latency_ms"),
                            "tool_call_id": payload.get("tool_call_id"),
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("activity.tool_use_write_failed", task_id=task_id, error=str(exc))

    async def _on_litellm_post(self, payload: dict[str, Any]) -> None:
        """Write an ``llm_call`` row."""
        task_id = payload.get("task_id")
        try:
            async with open_session(self.sessionmaker) as session:
                session.add(
                    Event(
                        task_id=task_id,
                        event_type="llm_call",
                        payload_json={
                            "model": payload.get("model"),
                            "outcome": payload.get("outcome", "success"),
                            "span_id": payload.get("span_id"),
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("activity.llm_call_write_failed", task_id=task_id, error=str(exc))
