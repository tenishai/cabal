"""ActivityLogger — durable per-activity rows in the events table.

Registers hook handlers for TOOL_USE, LITELLM_POST, and SESSION_END and
writes rows to the existing ``events`` table (models.Event). Unlike
checkpoints, these rows are NEVER dropped on task success — that's the
whole point.

Design decisions
----------------
* **Two rows per tool call** (``tool_call`` + ``tool_result``) rather than
  one combined row.  Rationale is *schema cleanliness*: each row stays
  single-purpose — ``tool_call`` records intent (name + arguments),
  ``tool_result`` records outcome (result, latency, error flag) — which
  keeps per-type queries clean ("all invocations", "all errors", "p95
  latency") and gives #51's token/cost a clean home on ``llm_call``.
  NOTE: the rows are written *atomically together* from a single
  post-``call_tool`` ``TOOL_USE`` fire, so this split does **not** by
  itself detect a "call present, result absent" crash gap — earning that
  requires a genuine *pre*-``call_tool`` fire point, which is issue #52's
  job (the pre-tool veto/patch hook).

* **Sync mode is deliberate, not a default to revisit.** This is an audit
  log; durability is the whole point.  In this registry ``async`` mode is
  true fire-and-forget (``asyncio.create_task``, see registry.py) — a write
  could be lost if the loop tears down before the task runs, which is
  exactly the failure an audit log must not have.  ``sync`` awaits the write
  to completion before the dispatch proceeds.  The cost is inline latency;
  concurrent writers to the shared SQLite file are serialised by WAL +
  ``busy_timeout`` (proven by ``test_concurrent_writes_all_land``).

* **Tokens / cost** are explicitly absent — that is scope #B4 / issue #51.
  Do not add them here.

* **entry_id / parent_id tree shape** (#50): every row carries an
  ``entry_id`` (12-char hex, stable per row) and a ``parent_id`` (the
  previous entry in the same task's emission sequence, or ``None`` for the
  first).  Both live inside ``payload_json`` — no new DB columns, no
  migration.  The per-task last-entry map (``_last_entry_ids``) is keyed by
  ``task_id`` and cleaned up on ``SESSION_END``.

  Emission order within one full tool-call dispatch:
    1. LITELLM_POST (initial completion)  → ``llm_call``  parent=None
    2. TOOL_USE (per-call pair)           → ``tool_call`` parent=llm_call.entry_id
                                          → ``tool_result`` parent=tool_call.entry_id
    3. LITELLM_POST (post-tool completion) → ``llm_call`` parent=tool_result.entry_id

  Concurrent *different* tasks are isolated by the task_id key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import open_session
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.models import Event

log = structlog.get_logger(__name__)


def _new_entry_id() -> str:
    """Generate a short stable id for a single activity row (12-char hex)."""
    return uuid4().hex[:12]


def path_to_root(events: list[Any], leaf_entry_id: str) -> list[Any]:
    """Walk the parent_id chain from ``leaf_entry_id`` back to the root.

    Builds an in-memory ``{entry_id: event}`` map from ``events`` (a list of
    ``models.Event`` rows for a single task) and walks ``parent_id`` from the
    leaf back to root.  Returns the chain in **root→leaf** order so index 0
    is the dispatch origin.

    Handles missing/None ``parent_id`` cleanly (stops the walk); a cycle
    guard prevents infinite loops in corrupt data.
    """
    by_entry_id: dict[str, Any] = {}
    for event in events:
        payload = event.payload_json or {}
        eid = payload.get("entry_id")
        if eid is not None:
            by_entry_id[eid] = event

    chain: list[Any] = []
    current_id: str | None = leaf_entry_id
    seen: set[str] = set()
    while current_id is not None:
        if current_id in seen:
            break  # cycle guard
        seen.add(current_id)
        event = by_entry_id.get(current_id)
        if event is None:
            break
        chain.append(event)
        current_id = (event.payload_json or {}).get("parent_id")

    chain.reverse()  # root → leaf
    return chain


@dataclass
class ActivityLogger:
    """Wire the TOOL_USE, LITELLM_POST, and SESSION_END hooks to durable event rows.

    Pass ``sessionmaker`` — the same one wired to the rest of the app —
    so writes land in the same SQLite file as all other events.

    ``_last_entry_ids`` is a per-task map of the most-recently-written
    ``entry_id``, used to chain ``parent_id`` across hook firings within the
    same task.  It is pruned on ``SESSION_END`` so it doesn't grow unbounded.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    mode: str = "sync"
    _last_entry_ids: dict[str, str] = field(default_factory=dict, init=False)

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
        registry.register(
            HookName.SESSION_END,
            self._on_session_end,
            mode=self.mode,
            label="activity.session_end",
        )

    async def _on_tool_use(self, payload: dict[str, Any]) -> None:
        """Write a ``tool_call`` row and a ``tool_result`` row.

        Both rows are written in a single DB transaction.  The ``tool_call``
        row's ``parent_id`` points at the previous entry for this task (e.g.
        the initial ``llm_call``); the ``tool_result`` row's ``parent_id``
        points at the ``tool_call`` row.  The map is updated to the
        ``tool_result``'s ``entry_id`` so the next row (usually another
        ``llm_call``) chains correctly.
        """
        task_id = payload.get("task_id")
        tool_name = payload.get("tool_name", "")

        # Generate stable ids for both rows before the write.
        tc_entry_id = _new_entry_id()
        tr_entry_id = _new_entry_id()
        prior_entry_id = self._last_entry_ids.get(task_id) if task_id else None

        # Update the map: after this handler the last entry for this task is
        # the tool_result row.
        if task_id is not None:
            self._last_entry_ids[task_id] = tr_entry_id

        try:
            async with open_session(self.sessionmaker) as session:
                session.add(
                    Event(
                        task_id=task_id,
                        event_type="tool_call",
                        payload_json={
                            "entry_id": tc_entry_id,
                            "parent_id": prior_entry_id,
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
                            "entry_id": tr_entry_id,
                            "parent_id": tc_entry_id,
                            "tool_name": tool_name,
                            "result_summary": payload.get("result_summary"),
                            "result_truncated": payload.get("result_truncated", False),
                            "is_error": payload.get("is_error", False),
                            "latency_ms": payload.get("latency_ms"),
                            "tool_call_id": payload.get("tool_call_id"),
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("activity.tool_use_write_failed", task_id=task_id, error=str(exc))

    async def _on_litellm_post(self, payload: dict[str, Any]) -> None:
        """Write an ``llm_call`` row.

        NOTE: ``LITELLM_POST`` fires on the budget-exhausted and circuit-open
        branches *before* the provider is called (litellm_client.py), so an
        ``llm_call`` row does not always mean a provider round-trip happened.
        Consult ``outcome`` before counting rows as provider invocations.
        """
        task_id = payload.get("task_id")

        lc_entry_id = _new_entry_id()
        prior_entry_id = self._last_entry_ids.get(task_id) if task_id else None

        if task_id is not None:
            self._last_entry_ids[task_id] = lc_entry_id

        try:
            async with open_session(self.sessionmaker) as session:
                session.add(
                    Event(
                        task_id=task_id,
                        event_type="llm_call",
                        payload_json={
                            "entry_id": lc_entry_id,
                            "parent_id": prior_entry_id,
                            "model": payload.get("model"),
                            "outcome": payload.get("outcome", "success"),
                            "span_id": payload.get("span_id"),
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("activity.llm_call_write_failed", task_id=task_id, error=str(exc))

    async def _on_session_end(self, payload: dict[str, Any]) -> None:
        """Remove the task's last-entry-id from the map when the session ends.

        Keeps ``_last_entry_ids`` from growing unbounded across many tasks.
        Silently tolerates a missing or already-absent ``task_id``.
        """
        task_id = payload.get("task_id")
        if task_id is not None:
            self._last_entry_ids.pop(task_id, None)
