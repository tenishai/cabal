"""ActivityLogger — durable per-activity rows in the events table.

Registers hook handlers for TOOL_USE and LITELLM_POST and writes rows
to the existing ``events`` table (models.Event). Unlike checkpoints,
these rows are NEVER dropped on task success — that's the whole point.

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

* **Tokens / cost** (#51 / B4): the ``LITELLM_POST`` success payload carries
  both ``tokens_used`` (scalar int total, from ``_tokens_used()``) and
  ``response`` (the raw LiteLLM ModelResponse).  ``_on_litellm_post`` reads
  the prompt/completion breakdown from ``response.usage`` when available and
  falls back to the scalar total; cost comes from
  ``response._hidden_params["response_cost"]``.  Both are stored as ``null``
  when unavailable — a missing-cost or missing-usage must never drop the row.
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


def _extract_usage(response: Any) -> dict[str, int] | None:
    """Extract token usage breakdown from a LiteLLM/OpenAI response object.

    Returns ``{"input": N, "output": N, "total": N}`` when the usage block is
    present, or ``None`` when it is absent / malformed.  Never raises.
    """
    try:
        if response is None:
            return None
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        if usage is None:
            return None
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            total = usage.get("total_tokens")
        else:
            prompt = getattr(usage, "prompt_tokens", None)
            completion = getattr(usage, "completion_tokens", None)
            total = getattr(usage, "total_tokens", None)
        if prompt is None and completion is None and total is None:
            return None
        result: dict[str, int] = {}
        if prompt is not None:
            result["input"] = int(prompt)
        if completion is not None:
            result["output"] = int(completion)
        if total is not None:
            result["total"] = int(total)
        elif "input" in result and "output" in result:
            result["total"] = result["input"] + result["output"]
        return result or None
    except Exception:  # noqa: BLE001
        return None


def _extract_cost(response: Any) -> float | None:
    """Extract response cost from LiteLLM's hidden params.

    LiteLLM stores the computed cost on ``response._hidden_params["response_cost"]``
    after a successful provider call.  Returns ``None`` when unavailable.
    Never raises.
    """
    if response is None:
        return None
    try:
        hidden = getattr(response, "_hidden_params", None)
        if isinstance(hidden, dict):
            cost = hidden.get("response_cost")
            if cost is not None:
                return float(cost)
    except Exception:  # noqa: BLE001
        pass
    return None


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

        Token usage is sourced from ``payload["response"].usage`` (breakdown)
        with ``payload["tokens_used"]`` (scalar total) as a fallback.  Cost
        comes from ``response._hidden_params["response_cost"]``.  Both are
        stored as ``null`` when unavailable — never raises.
        """
        task_id = payload.get("task_id")
        try:
            response = payload.get("response")
            # `tokens` is always a dict-or-None so consumers can rely on
            # tokens["total"]: prefer the response.usage breakdown, else
            # normalise the scalar tokens_used fallback into {"total": N}.
            tokens = _extract_usage(response)
            if tokens is None:
                scalar = payload.get("tokens_used")
                if scalar is not None:
                    try:
                        tokens = {"total": int(scalar)}
                    except (TypeError, ValueError):
                        tokens = None  # never let a bad scalar drop the row
            cost = _extract_cost(response)

            async with open_session(self.sessionmaker) as session:
                session.add(
                    Event(
                        task_id=task_id,
                        event_type="llm_call",
                        payload_json={
                            "model": payload.get("model"),
                            "outcome": payload.get("outcome", "success"),
                            "span_id": payload.get("span_id"),
                            "tokens": tokens,
                            "cost": cost,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("activity.llm_call_write_failed", task_id=task_id, error=str(exc))
