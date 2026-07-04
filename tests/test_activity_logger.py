"""ActivityLogger — durable tool_call / tool_result / llm_call rows.

Asserts that after a persona completes a task issuing one tool call:
  - the events table contains a ``tool_call`` row with {tool_name, arguments}
  - the events table contains a ``tool_result`` row with {tool_name,
    result_summary, is_error, latency_ms}
  - the events table contains an ``llm_call`` row with {model, outcome}
  - all three rows survive task success (not dropped with the checkpoint)

Also verifies the pure hook-routing layer (TOOL_USE → two rows,
LITELLM_POST → one llm_call row) independently of the persona machinery.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.models import Event
from cabal.core.observability.activity_logger import ActivityLogger

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


class _FakeOutcome:
    """Minimal duck-typed ToolCallOutcome for tests (no limenal dep)."""

    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [{"type": "text", "text": text}]
        self.is_error = is_error


class _FakeToolProxy:
    """Returns a single known tool then a stub outcome."""

    def __init__(self, result_text: str = "42", *, is_error: bool = False) -> None:
        self._result = _FakeOutcome(result_text, is_error=is_error)
        self.calls: list[dict[str, Any]] = []

    async def list_tools_for_llm(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "calculator__add",
                    "description": "Add two numbers",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeOutcome:
        self.calls.append({"name": name, "arguments": arguments})
        return self._result


def _tool_call_stub(final_reply: str = "done"):
    """LLM stub: first call returns a tool_call, second returns text."""
    state = {"step": 0}

    async def fn(model: str, messages: list, **kwargs) -> dict:
        state["step"] += 1
        if state["step"] == 1 and "tools" in kwargs:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-001",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator__add",
                                        "arguments": json.dumps({"a": 1, "b": 2}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": final_reply}}]}

    return fn


def _user_task(task_id: str = "t-act-1", text: str = "add 1 and 2") -> Task:
    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


# ---------------------------------------------------------------------------
# Pure hook-routing tests (no DB, no full persona run)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_use_hook_writes_two_rows(app: FastAPI) -> None:
    """Firing TOOL_USE via a freshly-registered ActivityLogger produces two rows."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    await registry.fire(
        HookName.TOOL_USE,
        {
            "task_id": "hook-task-1",
            "tool_call_id": "c-1",
            "tool_name": "my_tool",
            "arguments": {"x": 1},
            "result_summary": "result text",
            "is_error": False,
            "latency_ms": 123,
        },
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == "hook-task-1")
            )
        ).scalars().all()

    types = {r.event_type for r in rows}
    assert types == {"tool_call", "tool_result"}

    by_type = {r.event_type: r.payload_json for r in rows}
    assert by_type["tool_call"]["tool_name"] == "my_tool"
    assert by_type["tool_call"]["arguments"] == {"x": 1}
    assert by_type["tool_result"]["is_error"] is False
    assert by_type["tool_result"]["latency_ms"] == 123
    assert by_type["tool_result"]["result_summary"] == "result text"


@pytest.mark.asyncio
async def test_litellm_post_hook_writes_llm_call_row(app: FastAPI) -> None:
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    await registry.fire(
        HookName.LITELLM_POST,
        {
            "task_id": "hook-task-2",
            "model": "anthropic/claude-sonnet-4-6",
            "outcome": "success",
            "span_id": "sp-abc",
        },
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == "hook-task-2")
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].event_type == "llm_call"
    payload = rows[0].payload_json
    assert payload["model"] == "anthropic/claude-sonnet-4-6"
    assert payload["outcome"] == "success"
    assert payload["span_id"] == "sp-abc"


# ---------------------------------------------------------------------------
# End-to-end persona dispatch test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_dispatch_writes_all_three_row_types(app: FastAPI) -> None:
    """Full dispatch with a stub tool proxy produces tool_call, tool_result,
    and llm_call rows that survive task success."""
    from cabal.agents.llm_persona import LLMPersona

    sessionmaker = app.state.sessionmaker
    task_id = "t-e2e-activity"

    # Wire a fresh registry with ActivityLogger so rows land in the test DB.
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    llm_client = LiteLLMClient(
        acompletion_fn=_tool_call_stub("The answer is 3."),
        hook_registry=registry,
    )
    proxy = _FakeToolProxy(result_text="3")
    persona = LLMPersona(
        name="calculator",
        model="test/model",
        llm_client=llm_client,
        system_prompt="You are a calculator.",
        tool_proxy=proxy,
    )

    task = _user_task(task_id=task_id, text="what is 1+2?")
    result = await persona.handle_task(task)

    # Task must have succeeded.
    assert result.status.state == "completed", result.status

    # Query events for our task.
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == task_id)
            )
        ).scalars().all()

    by_type = {}
    for r in rows:
        by_type.setdefault(r.event_type, []).append(r.payload_json)

    # Cardinality is pinned, not just membership: one tool call yields exactly
    # one tool_call + one tool_result, and TWO llm_call rows (the initial
    # completion that emitted the tool call, plus the post-tool completion).
    assert len(by_type.get("tool_call", [])) == 1, by_type
    assert len(by_type.get("tool_result", [])) == 1, by_type
    assert len(by_type.get("llm_call", [])) == 2, by_type

    # tool_call row must record the tool name and arguments.
    tc = by_type["tool_call"][0]
    assert tc["tool_name"] == "calculator__add"
    assert tc["arguments"] == {"a": 1, "b": 2}

    # tool_result row must record outcome fields.
    tr = by_type["tool_result"][0]
    assert tr["tool_name"] == "calculator__add"
    assert tr["result_summary"] == "3"
    assert tr["is_error"] is False
    assert tr["result_truncated"] is False
    assert isinstance(tr["latency_ms"], int)

    # llm_call row must record model + outcome.
    lc = by_type["llm_call"][0]
    assert lc["model"] == "test/model"
    assert lc["outcome"] == "success"


@pytest.mark.asyncio
async def test_activity_rows_survive_task_success(app: FastAPI) -> None:
    """Checkpoint is dropped on success; event rows must remain."""
    from cabal.agents.llm_persona import LLMPersona
    from cabal.core.checkpoint import SqliteCheckpointStore

    sessionmaker = app.state.sessionmaker
    task_id = "t-survive"

    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    llm_client = LiteLLMClient(
        acompletion_fn=_tool_call_stub("survived!"),
        hook_registry=registry,
    )
    checkpoint_store = SqliteCheckpointStore(sessionmaker=sessionmaker)
    persona = LLMPersona(
        name="survivor",
        model="test/model",
        llm_client=llm_client,
        system_prompt="survive.",
        tool_proxy=_FakeToolProxy("ok"),
        checkpoint_store=checkpoint_store,
    )

    result = await persona.handle_task(_user_task(task_id=task_id))
    assert result.status.state == "completed"

    # Checkpoint was dropped on success.
    loaded_ckpt = await checkpoint_store.load(task_id)
    assert loaded_ckpt is None, "checkpoint should be gone after success"

    # Event rows must still be there.
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == task_id)
            )
        ).scalars().all()

    surviving_types = {r.event_type for r in rows}
    assert "tool_call" in surviving_types
    assert "tool_result" in surviving_types
    assert "llm_call" in surviving_types


@pytest.mark.asyncio
async def test_error_outcome_is_recorded(app: FastAPI) -> None:
    """When a tool returns is_error=True, the tool_result row reflects it."""
    from cabal.agents.llm_persona import LLMPersona

    sessionmaker = app.state.sessionmaker
    task_id = "t-err"

    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    llm_client = LiteLLMClient(
        acompletion_fn=_tool_call_stub("handled error"),
        hook_registry=registry,
    )
    persona = LLMPersona(
        name="erring",
        model="test/model",
        llm_client=llm_client,
        system_prompt="err.",
        tool_proxy=_FakeToolProxy("tool error: boom", is_error=True),
    )

    result = await persona.handle_task(_user_task(task_id=task_id))
    assert result.status.state == "completed"

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event)
                .where(Event.task_id == task_id)
                .where(Event.event_type == "tool_result")
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].payload_json["is_error"] is True
    assert "tool error: boom" in rows[0].payload_json["result_summary"]


@pytest.mark.asyncio
async def test_result_summary_truncation_is_marked(app: FastAPI) -> None:
    """An over-long tool result is capped at 2000 chars and flagged truncated;
    a short one is stored whole with result_truncated=False."""
    from cabal.agents.llm_persona import _RESULT_SUMMARY_MAX, LLMPersona

    sessionmaker = app.state.sessionmaker
    task_id = "t-trunc"

    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    huge = "x" * (_RESULT_SUMMARY_MAX + 500)
    persona = LLMPersona(
        name="verbose",
        model="test/model",
        llm_client=LiteLLMClient(
            acompletion_fn=_tool_call_stub("done"), hook_registry=registry
        ),
        system_prompt="verbose.",
        tool_proxy=_FakeToolProxy(huge),
    )

    result = await persona.handle_task(_user_task(task_id=task_id))
    assert result.status.state == "completed"

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event)
                .where(Event.task_id == task_id)
                .where(Event.event_type == "tool_result")
            )
        ).scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload_json
    assert len(payload["result_summary"]) == _RESULT_SUMMARY_MAX
    assert payload["result_truncated"] is True


@pytest.mark.asyncio
async def test_concurrent_writes_all_land(app: FastAPI) -> None:
    """Issue #2 guard: many concurrent TOOL_USE writers against the shared
    SQLite file must ALL land — no dropped rows under WAL/busy_timeout
    contention. Proves the sync-mode durability claim rather than assuming it.
    """
    import asyncio

    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    n = 40

    async def _fire(i: int) -> None:
        await registry.fire(
            HookName.TOOL_USE,
            {
                "task_id": f"t-conc-{i}",
                "tool_call_id": f"c-{i}",
                "tool_name": "concurrent_tool",
                "arguments": {"i": i},
                "result_summary": f"result-{i}",
                "is_error": False,
                "latency_ms": i,
            },
        )

    await asyncio.gather(*(_fire(i) for i in range(n)))

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.event_type.in_(("tool_call", "tool_result")))
            )
        ).scalars().all()

    conc = [r for r in rows if str(r.task_id).startswith("t-conc-")]
    # Each of n fires writes exactly 2 rows (tool_call + tool_result).
    assert len(conc) == 2 * n, f"expected {2 * n} rows, got {len(conc)}"
    task_ids = {r.task_id for r in conc}
    assert len(task_ids) == n, "every concurrent writer must have landed"
