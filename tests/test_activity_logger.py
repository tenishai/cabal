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


# ---------------------------------------------------------------------------
# Token + cost tests (issue #51 / B4)
# ---------------------------------------------------------------------------


class _FakeUsage:
    """Minimal duck-typed litellm Usage object."""

    def __init__(self, *, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _FakeLiteLLMResponse:
    """Minimal duck-typed litellm ModelResponse with usage + _hidden_params."""

    def __init__(
        self,
        *,
        prompt_tokens: int = 10,
        completion_tokens: int = 20,
        total_tokens: int = 30,
        response_cost: float | None = 0.001,
    ) -> None:
        self.usage = _FakeUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        self._hidden_params: dict[str, Any] = {}
        if response_cost is not None:
            self._hidden_params["response_cost"] = response_cost


@pytest.mark.asyncio
async def test_llm_call_row_records_token_counts(app: FastAPI) -> None:
    """LITELLM_POST with a stub response that has a usage block records token breakdown."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    fake_response = _FakeLiteLLMResponse(
        prompt_tokens=15, completion_tokens=25, total_tokens=40, response_cost=None
    )
    await registry.fire(
        HookName.LITELLM_POST,
        {
            "task_id": "tok-task-1",
            "model": "test/model",
            "outcome": "success",
            "span_id": "sp-tok",
            "response": fake_response,
            "tokens_used": 40,
        },
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == "tok-task-1")
            )
        ).scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload_json
    assert payload["tokens"] is not None, "tokens must not be null when usage is available"
    assert payload["tokens"]["input"] == 15
    assert payload["tokens"]["output"] == 25
    assert payload["tokens"]["total"] == 40


@pytest.mark.asyncio
async def test_llm_call_row_records_cost_when_available(app: FastAPI) -> None:
    """LITELLM_POST with a response carrying _hidden_params[response_cost] stores it."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    fake_response = _FakeLiteLLMResponse(
        prompt_tokens=10, completion_tokens=20, total_tokens=30, response_cost=0.00042
    )
    await registry.fire(
        HookName.LITELLM_POST,
        {
            "task_id": "cost-task-1",
            "model": "test/model",
            "outcome": "success",
            "span_id": None,
            "response": fake_response,
            "tokens_used": 30,
        },
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == "cost-task-1")
            )
        ).scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload_json
    assert payload["cost"] == pytest.approx(0.00042)


@pytest.mark.asyncio
async def test_llm_call_row_null_cost_when_unavailable(app: FastAPI) -> None:
    """When the response has no cost info, cost is null — the row must still be written."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    fake_response = _FakeLiteLLMResponse(
        prompt_tokens=5, completion_tokens=10, total_tokens=15, response_cost=None
    )
    await registry.fire(
        HookName.LITELLM_POST,
        {
            "task_id": "nocost-task-1",
            "model": "test/model",
            "outcome": "success",
            "span_id": None,
            "response": fake_response,
            "tokens_used": 15,
        },
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == "nocost-task-1")
            )
        ).scalars().all()

    # Row must exist even though cost is absent.
    assert len(rows) == 1
    payload = rows[0].payload_json
    assert payload["cost"] is None
    # Tokens must still be recorded.
    assert payload["tokens"] is not None
    assert payload["tokens"]["total"] == 15


@pytest.mark.asyncio
async def test_scalar_tokens_fallback_is_normalised_to_dict(app: FastAPI) -> None:
    """With no response object, the scalar tokens_used fallback is normalised
    to {"total": N} so consumers can always rely on tokens["total"]."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    await registry.fire(
        HookName.LITELLM_POST,
        {
            "task_id": "scalar-task-1",
            "model": "test/model",
            "outcome": "success",
            "span_id": None,
            # no "response" key → _extract_usage returns None → scalar fallback
            "tokens_used": 77,
        },
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event).where(Event.task_id == "scalar-task-1")
            )
        ).scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload_json
    assert payload["tokens"] == {"total": 77}, payload["tokens"]
    assert payload["cost"] is None


@pytest.mark.asyncio
async def test_token_sum_per_task_id(app: FastAPI) -> None:
    """A SELECT can sum tokens per task_id across multiple llm_call rows."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    task_id = "sum-task-1"

    # Fire two llm_call events for the same task_id with known token totals.
    for i, (prompt, completion, total, cost) in enumerate(
        [(10, 20, 30, 0.001), (5, 15, 20, 0.0005)]
    ):
        fake_response = _FakeLiteLLMResponse(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            response_cost=cost,
        )
        await registry.fire(
            HookName.LITELLM_POST,
            {
                "task_id": task_id,
                "model": "test/model",
                "outcome": "success",
                "span_id": f"sp-{i}",
                "response": fake_response,
                "tokens_used": total,
            },
        )

    # Pull all llm_call rows for the task and sum tokens["total"] in Python
    # (SQLite JSON path queries are dialect-specific; summing in Python is
    # simpler and proves the data was stored correctly).
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Event)
                .where(Event.task_id == task_id)
                .where(Event.event_type == "llm_call")
            )
        ).scalars().all()

    assert len(rows) == 2
    token_totals = [r.payload_json["tokens"]["total"] for r in rows]
    assert sum(token_totals) == 50, f"expected 50 total tokens, got {sum(token_totals)}"


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


# ---------------------------------------------------------------------------
# #50 — entry_id / parent_id tree-shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_row_carries_entry_id_and_parent_id(app) -> None:
    """Every emitted event row has an entry_id and a parent_id in payload_json."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    task_id = "t-tree-fields"

    # Fire LITELLM_POST — one llm_call row.
    await registry.fire(
        HookName.LITELLM_POST,
        {"task_id": task_id, "model": "test/m", "outcome": "success"},
    )
    # Fire TOOL_USE — two rows (tool_call + tool_result).
    await registry.fire(
        HookName.TOOL_USE,
        {
            "task_id": task_id,
            "tool_call_id": "c-1",
            "tool_name": "my_tool",
            "arguments": {"x": 1},
            "result_summary": "ok",
            "is_error": False,
            "latency_ms": 5,
        },
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(select(Event).where(Event.task_id == task_id))
        ).scalars().all()

    assert len(rows) == 3
    for row in rows:
        payload = row.payload_json or {}
        assert "entry_id" in payload, f"missing entry_id on {row.event_type}"
        assert "parent_id" in payload, f"missing parent_id on {row.event_type}"
        assert isinstance(payload["entry_id"], str)
        assert len(payload["entry_id"]) == 12


@pytest.mark.asyncio
async def test_first_row_in_dispatch_has_null_parent(app) -> None:
    """The very first row written for a task has parent_id=None."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    task_id = "t-first-parent"

    await registry.fire(
        HookName.LITELLM_POST,
        {"task_id": task_id, "model": "test/m", "outcome": "success"},
    )

    async with sessionmaker() as session:
        rows = (
            await session.execute(select(Event).where(Event.task_id == task_id))
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].payload_json["parent_id"] is None


@pytest.mark.asyncio
async def test_path_to_root_single_tool_call_dispatch(app) -> None:
    """path_to_root returns the correct root→leaf chain for a full dispatch.

    Emission order for a single-tool-call dispatch is:
      llm_call_1 (initial completion, parent=None)
      → tool_call  (parent=llm_call_1)
      → tool_result (parent=tool_call)
      → llm_call_2 (post-tool completion, parent=tool_result)

    path_to_root(events, llm_call_2.entry_id) must return that chain in
    root→leaf order: [llm_call_1, tool_call, tool_result, llm_call_2].
    """
    from cabal.agents.llm_persona import LLMPersona
    from cabal.core.observability.activity_logger import path_to_root

    sessionmaker = app.state.sessionmaker
    task_id = "t-path-to-root"

    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    llm_client = LiteLLMClient(
        acompletion_fn=_tool_call_stub("done"),
        hook_registry=registry,
    )
    persona = LLMPersona(
        name="path-tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=_FakeToolProxy("result"),
    )

    result = await persona.handle_task(_user_task(task_id=task_id))
    assert result.status.state == "completed"

    async with sessionmaker() as session:
        events = (
            await session.execute(select(Event).where(Event.task_id == task_id))
        ).scalars().all()

    # Expect 4 rows: llm_call_1, tool_call, tool_result, llm_call_2.
    assert len(events) == 4

    # Find the last llm_call row (highest DB id since they're append-only).
    llm_calls = [e for e in events if e.event_type == "llm_call"]
    assert len(llm_calls) == 2
    last_llm_call = max(llm_calls, key=lambda e: e.id)

    chain = path_to_root(events, last_llm_call.payload_json["entry_id"])

    # Must be 4 events, root→leaf.
    assert len(chain) == 4, f"expected 4 events in chain, got {len(chain)}: {[e.event_type for e in chain]}"
    assert chain[0].event_type == "llm_call"   # root: initial completion
    assert chain[1].event_type == "tool_call"
    assert chain[2].event_type == "tool_result"
    assert chain[3].event_type == "llm_call"   # leaf: post-tool completion

    # Verify parent linkage is consistent.
    for i in range(1, len(chain)):
        parent_id = chain[i].payload_json["parent_id"]
        expected_parent_entry_id = chain[i - 1].payload_json["entry_id"]
        assert parent_id == expected_parent_entry_id, (
            f"chain[{i}].parent_id={parent_id!r} does not match "
            f"chain[{i-1}].entry_id={expected_parent_entry_id!r}"
        )


@pytest.mark.asyncio
async def test_concurrent_tasks_have_independent_parent_chains(app) -> None:
    """Two concurrent tasks must not bleed parent_ids into each other's chains."""
    import asyncio

    from cabal.agents.llm_persona import LLMPersona
    from cabal.core.observability.activity_logger import path_to_root

    sessionmaker = app.state.sessionmaker

    # Single ActivityLogger instance shared across both tasks (realistic case).
    registry = HookRegistry()
    logger = ActivityLogger(sessionmaker=sessionmaker, mode="sync")
    logger.register(registry)

    async def _run_task(task_id: str) -> None:
        llm_client = LiteLLMClient(
            acompletion_fn=_tool_call_stub(f"done-{task_id}"),
            hook_registry=registry,
        )
        persona = LLMPersona(
            name="concurrent-tester",
            model="test/model",
            llm_client=llm_client,
            system_prompt="test",
            tool_proxy=_FakeToolProxy("ok"),
        )
        result = await persona.handle_task(_user_task(task_id=task_id))
        assert result.status.state == "completed", f"task {task_id} failed"

    task_ids = ["t-conc-chain-A", "t-conc-chain-B"]
    await asyncio.gather(*(_run_task(tid) for tid in task_ids))

    for tid in task_ids:
        async with sessionmaker() as session:
            events = (
                await session.execute(select(Event).where(Event.task_id == tid))
            ).scalars().all()

        assert len(events) == 4, f"task {tid}: expected 4 events, got {len(events)}"

        # Each task's chain must be internally consistent (no cross-task leakage).
        llm_calls = [e for e in events if e.event_type == "llm_call"]
        last_llm = max(llm_calls, key=lambda e: e.id)
        chain = path_to_root(events, last_llm.payload_json["entry_id"])
        assert len(chain) == 4, f"task {tid}: path_to_root returned {len(chain)} events"

        # All events in the chain must belong to this task.
        for evt in chain:
            assert evt.task_id == tid, f"cross-task bleed: {evt.task_id!r} in chain for {tid!r}"


@pytest.mark.asyncio
async def test_session_end_clears_last_entry_map(app) -> None:
    """SESSION_END removes the task from the last-entry-id map."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    logger = ActivityLogger(sessionmaker=sessionmaker, mode="sync")
    logger.register(registry)

    task_id = "t-ses-end-cleanup"

    # Write one row to populate the map.
    await registry.fire(
        HookName.LITELLM_POST,
        {"task_id": task_id, "model": "test/m", "outcome": "success"},
    )
    assert task_id in logger._last_entry_ids

    # Fire SESSION_END.
    await registry.fire(HookName.SESSION_END, {"task_id": task_id, "state": "completed"})
    assert task_id not in logger._last_entry_ids


@pytest.mark.asyncio
async def test_session_end_missing_task_id_does_not_error(app) -> None:
    """SESSION_END with no task_id must not raise."""
    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    # Must not raise even though no task_id is in the map.
    await registry.fire(HookName.SESSION_END, {"state": "completed"})


@pytest.mark.asyncio
async def test_path_to_root_no_tool_calls(app) -> None:
    """path_to_root for a dispatch with zero tool calls returns just the llm_call."""
    from cabal.core.observability.activity_logger import path_to_root

    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    ActivityLogger(sessionmaker=sessionmaker, mode="sync").register(registry)

    task_id = "t-no-tools"

    # One LITELLM_POST, no TOOL_USE (simulates a no-tool-call dispatch).
    await registry.fire(
        HookName.LITELLM_POST,
        {"task_id": task_id, "model": "test/m", "outcome": "success"},
    )

    async with sessionmaker() as session:
        events = (
            await session.execute(select(Event).where(Event.task_id == task_id))
        ).scalars().all()

    assert len(events) == 1
    leaf_entry_id = events[0].payload_json["entry_id"]
    chain = path_to_root(events, leaf_entry_id)
    assert len(chain) == 1
    assert chain[0].event_type == "llm_call"


@pytest.mark.asyncio
async def test_dropped_write_does_not_orphan_the_chain(app) -> None:
    """A dropped write must NOT advance the parent chain: the next successful
    row reconnects to the last *committed* entry, skipping only the lost node
    rather than orphaning the whole tail from root (Prudence #55, Important)."""
    from cabal.core.observability.activity_logger import path_to_root

    sessionmaker = app.state.sessionmaker
    registry = HookRegistry()
    logger = ActivityLogger(sessionmaker=sessionmaker, mode="sync")
    logger.register(registry)

    task_id = "t-orphan"

    # 1) A successful llm_call → map now holds entry A.
    await registry.fire(
        HookName.LITELLM_POST,
        {"task_id": task_id, "model": "test/m", "outcome": "success"},
    )
    entry_a = logger._last_entry_ids[task_id]

    # 2) Force the next write to fail by swapping in a sessionmaker that raises
    #    when called (open_session does `async with sessionmaker() as session`).
    class _BoomSessionmaker:
        def __call__(self, *a, **k):
            raise RuntimeError("boom")

    logger.sessionmaker = _BoomSessionmaker()
    await registry.fire(
        HookName.LITELLM_POST,
        {"task_id": task_id, "model": "test/m", "outcome": "success"},
    )
    # The failed write must NOT have advanced the chain.
    assert logger._last_entry_ids[task_id] == entry_a

    # 3) Restore and fire again — this row must parent to A (reconnected).
    logger.sessionmaker = sessionmaker
    await registry.fire(
        HookName.LITELLM_POST,
        {"task_id": task_id, "model": "test/m", "outcome": "success"},
    )

    async with sessionmaker() as session:
        events = (
            await session.execute(select(Event).where(Event.task_id == task_id))
        ).scalars().all()

    # Only the two successful rows were written; the dropped one left no row.
    assert len(events) == 2
    latest = max(events, key=lambda e: e.id)
    assert latest.payload_json["parent_id"] == entry_a
    # The chain from the leaf still reaches root cleanly (no orphan gap).
    chain = path_to_root(events, latest.payload_json["entry_id"])
    assert len(chain) == 2
