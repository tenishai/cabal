"""The token meter for federated agents (cabal issue 394, part 2)."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cabal.config.identity import MeterConfig
from cabal.core.app import create_app
from cabal.core.meter import (
    KIND_AMBIGUOUS,
    KIND_TASK,
    KIND_UNATTRIBUTED,
    UsageReader,
    key_sha256,
    usage_from_block,
)
from cabal.core.models import MeterSpend
from cabal.core.running import RunningTask

KEY = "k-wright1-0123456789"
KEY2 = "k-wright2-0123456789"


# ---- parsing ---------------------------------------------------------------------


def test_anthropic_usage_keeps_cache_apart() -> None:
    u = usage_from_block(
        {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_creation_input_tokens": 38792,
            "cache_read_input_tokens": 10,
        }
    )
    assert u is not None
    assert (u.input, u.output, u.cache_write, u.cache_read) == (2, 4, 38792, 10)
    assert u.charged == 6


def test_openai_usage_takes_cached_out_of_input() -> None:
    u = usage_from_block(
        {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 60},
        }
    )
    assert u is not None
    assert (u.input, u.output, u.cache_read) == (40, 7, 60)
    assert u.charged == 47


def test_no_usage_block() -> None:
    assert usage_from_block({"foo": 1}) is None
    assert usage_from_block(None) is None


def _sse(*events: dict) -> bytes:
    return b"".join(
        f"event: {e.get('type', 'x')}\ndata: {json.dumps(e)}\n\n".encode() for e in events
    )


ANTHROPIC_STREAM = _sse(
    {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": 3,
                "output_tokens": 1,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 0,
            }
        },
    },
    {"type": "content_block_delta", "delta": {"text": "hi"}},
    {"type": "message_delta", "usage": {"output_tokens": 9}},
    {"type": "message_stop"},
)


def test_stream_usage_across_chunk_boundaries() -> None:
    reader = UsageReader("text/event-stream; charset=utf-8")
    for i in range(0, len(ANTHROPIC_STREAM), 7):
        reader.feed(ANTHROPIC_STREAM[i : i + 7])
    u = reader.finish()
    assert u is not None
    assert (u.input, u.output, u.cache_write) == (3, 9, 500)


def test_json_body_usage() -> None:
    reader = UsageReader("application/json")
    reader.feed(b'{"choices": [], "usage": {"prompt_tokens": 5, ')
    reader.feed(b'"completion_tokens": 2}}')
    u = reader.finish()
    assert u is not None and u.charged == 7


def test_stream_without_usage() -> None:
    reader = UsageReader("text/event-stream")
    reader.feed(b'data: {"x": 1}\n\ndata: [DONE]\n\n')
    assert reader.finish() is None


def test_root_budget_needs_task_budget() -> None:
    with pytest.raises(ValueError, match="root_budget needs"):
        MeterConfig(root_budget=10)


# ---- the route -------------------------------------------------------------------


class Upstream:
    """Stands in for api.anthropic.com or a model server."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.usage = {"input_tokens": 8, "output_tokens": 4, "cache_read_input_tokens": 100}
        self.stream = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.stream:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=ANTHROPIC_STREAM
            )
        return httpx.Response(200, json={"type": "message", "usage": self.usage})


def _identity(
    path: Path, *, task_budget: int | None = None, root_budget: int | None = None
) -> None:
    lines = ["[meter]", "enabled = true"]
    if task_budget is not None:
        lines.append(f"task_budget = {task_budget}")
    if root_budget is not None:
        lines.append(f"root_budget = {root_budget}")
    for name, key in (("wright1", KEY), ("wright2", KEY2)):
        lines += [
            f"[meter.agents.{name}]",
            f'key_sha256 = "{key_sha256(key)}"',
            'upstream = "https://upstream.test/base"',
        ]
    path.write_text("\n".join(lines) + "\n")


async def _app(tmp_path: Path, **budgets: int) -> tuple[FastAPI, Upstream]:
    _identity(tmp_path / "identity.toml", **budgets)
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    app = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    return app, Upstream()


@pytest_asyncio.fixture
async def metered(request, tmp_path: Path):
    budgets = getattr(request, "param", {})
    app, upstream = await _app(tmp_path, **budgets)
    async with app.router.lifespan_context(app):
        meter = app.state.meter
        await meter.client.aclose()
        meter.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield app, c, upstream


async def _settled(app: FastAPI) -> None:
    pending = list(app.state.meter._pending)
    if pending:
        await asyncio.gather(*pending)


async def _rows(app: FastAPI) -> list[MeterSpend]:
    async with app.state.sessionmaker() as s:
        return list((await s.execute(select(MeterSpend))).scalars())


def _call(c: AsyncClient, key: str = KEY):
    return c.post(
        f"/meter/{key}/v1/messages?beta=true",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer sk-ant-oat-secret", "anthropic-beta": "oauth-2025-04-20"},
    )


def _run(app: FastAPI, task_id: str, agent: str, caller: str | None = None) -> RunningTask:
    task = RunningTask(
        task_id=task_id, target_agent=agent, caller_agent=caller, started_at=datetime.now(UTC)
    )
    app.state.dispatch_router.running.add(task)
    return task


async def test_meter_off_is_404(client: AsyncClient) -> None:
    r = await _call(client)
    assert r.status_code == 404


async def test_unknown_key_is_404(metered) -> None:
    _, c, upstream = metered
    r = await _call(c, key="nope")
    assert r.status_code == 404
    assert upstream.requests == []


async def test_pass_through_untouched(metered) -> None:
    app, c, upstream = metered
    r = await _call(c)
    assert r.status_code == 200
    assert r.json()["usage"]["input_tokens"] == 8
    (sent,) = upstream.requests
    assert str(sent.url) == "https://upstream.test/base/v1/messages?beta=true"
    assert sent.headers["authorization"] == "Bearer sk-ant-oat-secret"
    assert sent.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert sent.headers["accept-encoding"] == "identity"
    assert sent.headers["host"] == "upstream.test"
    await _settled(app)
    (row,) = await _rows(app)
    assert (row.agent, row.kind, row.task_id) == ("wright1", KIND_UNATTRIBUTED, "")
    assert (row.calls, row.input_tokens, row.output_tokens, row.cache_read_tokens) == (1, 8, 4, 100)


async def test_get_is_forwarded_not_counted(metered) -> None:
    app, c, upstream = metered
    r = await c.get(f"/meter/{KEY}/")
    assert r.status_code == 200
    assert len(upstream.requests) == 1
    await _settled(app)
    assert await _rows(app) == []


async def test_call_charged_to_running_task_and_root(metered) -> None:
    app, c, upstream = metered
    upstream.stream = True
    _run(app, "root-1", "primus")
    _run(app, "leg-1", "wright1", caller="agent:primus")
    r = await _call(c)
    assert r.status_code == 200
    assert b"message_delta" in r.content
    await _settled(app)
    (row,) = await _rows(app)
    assert (row.kind, row.task_id, row.root_id) == (KIND_TASK, "leg-1", "root-1")
    assert (row.input_tokens, row.output_tokens, row.cache_write_tokens) == (3, 9, 500)


async def test_task_end_closes_row(metered) -> None:
    app, c, _ = metered
    task = _run(app, "t-1", "wright1")
    await _call(c)
    await _settled(app)
    app.state.dispatch_router.running.remove("t-1", task)
    await _settled(app)
    (row,) = await _rows(app)
    assert row.closed_at is not None


async def test_two_tasks_is_ambiguous(metered) -> None:
    app, c, upstream = metered
    _run(app, "t-1", "wright1")
    _run(app, "t-2", "wright1")
    r = await _call(c)
    assert r.status_code == 200
    await _settled(app)
    (row,) = await _rows(app)
    assert (row.kind, row.task_id, row.calls) == (KIND_AMBIGUOUS, "", 1)


@pytest.mark.parametrize("metered", [{"task_budget": 10}], indirect=True)
async def test_spent_task_is_refused_with_400(metered) -> None:
    app, c, upstream = metered
    _run(app, "t-1", "wright1")
    assert (await _call(c)).status_code == 200  # 8 in + 4 out = 12, over 10
    await _settled(app)
    r = await _call(c)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert "token budget" in r.json()["error"]["message"]
    assert len(upstream.requests) == 1
    await _settled(app)
    (row,) = await _rows(app)
    assert (row.calls, row.refused_calls) == (1, 1)


@pytest.mark.parametrize("metered", [{"task_budget": 10}], indirect=True)
async def test_cache_is_not_charged(metered) -> None:
    app, c, upstream = metered
    upstream.usage = {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 90000}
    _run(app, "t-1", "wright1")
    for _ in range(3):
        assert (await _call(c)).status_code == 200
        await _settled(app)
    assert app.state.meter.budget.remaining("t-1") == 4


@pytest.mark.parametrize("metered", [{"task_budget": 10, "root_budget": 15}], indirect=True)
async def test_root_budget_caps_the_fan_out(metered) -> None:
    app, c, upstream = metered
    _run(app, "root-1", "primus")
    _run(app, "leg-1", "wright1", caller="agent:primus")
    _run(app, "leg-2", "wright2", caller="agent:primus")
    assert (await _call(c, KEY)).status_code == 200
    r = await _call(c, KEY2)
    assert r.status_code == 400
    assert "left for the fan-out" in r.json()["error"]["message"]
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("metered", [{"task_budget": 10}], indirect=True)
async def test_task_end_drops_its_budget(metered) -> None:
    app, c, _ = metered
    task = _run(app, "t-1", "wright1")
    await _call(c)
    await _settled(app)
    meter = app.state.meter
    assert "t-1" in meter._open
    app.state.dispatch_router.running.remove("t-1", task)
    await _settled(app)
    assert "t-1" not in meter._open
    assert not meter.budget.has_budget("t-1")


async def test_upstream_down_is_502(metered) -> None:
    app, c, _ = metered

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app.state.meter.client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    r = await _call(c)
    assert r.status_code == 502


@pytest.mark.parametrize("metered", [{"task_budget": 10, "root_budget": 100}], indirect=True)
@pytest.mark.parametrize("leg_first", [True, False])
async def test_a_metered_root_keeps_the_root_budget(metered, leg_first) -> None:
    """Prudence on PR 442: a root running on a metered agent must draw on root_budget,
    in either call order, not have it replaced by a task_budget."""
    app, c, upstream = metered
    upstream.usage = {"input_tokens": 1, "output_tokens": 1}
    _run(app, "root-1", "wright1")
    _run(app, "leg-1", "wright2", caller="agent:wright1")
    order = [KEY2, KEY] if leg_first else [KEY, KEY2]
    for key in order:
        assert (await _call(c, key)).status_code == 200
        await _settled(app)
    budget = app.state.meter.budget
    # 100 - 10 carved for the leg - 2 the root spent itself.
    assert budget.remaining("root-1") == 88
    assert budget.remaining("leg-1") == 8


@pytest.mark.parametrize("metered", [{"task_budget": 10, "root_budget": 3}], indirect=True)
async def test_a_spent_metered_root_quotes_the_root_budget(metered) -> None:
    """Prudence on PR 442: the refusal names the budget the root actually drew on."""
    app, c, _ = metered
    _run(app, "root-1", "wright1")
    assert (await _call(c)).status_code == 200
    await _settled(app)
    r = await _call(c)
    assert r.status_code == 400
    assert "(3 input + output tokens)" in r.json()["error"]["message"]


def test_access_log_hides_the_key() -> None:
    import logging

    from cabal.core.meter import MeterKeyFilter, redact_meter_key

    assert (
        redact_meter_key("/meter/abc-123/v1/messages?beta=true")
        == "/meter/<key>/v1/messages?beta=true"
    )
    assert redact_meter_key("/agents/x/a2a") == "/agents/x/a2a"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "POST", "/meter/SECRET-KEY/v1/messages", "1.1", 200),
        None,
    )
    assert MeterKeyFilter().filter(record)
    assert "SECRET-KEY" not in record.getMessage()
    assert "/meter/<key>/v1/messages" in record.getMessage()


async def test_meter_app_filters_the_access_log(metered) -> None:
    import logging

    from cabal.core.meter import MeterKeyFilter

    assert any(isinstance(f, MeterKeyFilter) for f in logging.getLogger("uvicorn.access").filters)
