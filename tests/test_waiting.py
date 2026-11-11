"""The "waiting" outcome, reason 705, and cabal continuing the agent (cabal issue 406, part 2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.a2a.codes import CODE_WAITING, CabalReason, make_status
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode, Conversation

UNTIL = timedelta(hours=1)


async def _token(app: FastAPI, **kw: Any) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    kw.setdefault("ttl_seconds", 86400)
    return mint(signing_key=stored, sub="agent:alice", aud="cabal-core", scope=["colleague"], **kw)


class _Waiter:
    """Stands in for echo: the first `waits` replies say "waiting", then it completes."""

    name = "echo"

    def __init__(self, waiting: dict[str, Any] | None, waits: int = 1) -> None:
        self.waiting = waiting
        self.waits = waits
        self.seen: list[Task] = []

    async def handle_task(self, task: Task) -> Task:
        self.seen.append(task)
        assert task.status.message is not None
        history = [*task.history, task.status.message]
        if len(self.seen) <= self.waits:
            metadata = {**task.metadata, "cabal_reason_code": CODE_WAITING}
            if self.waiting is not None:
                metadata["cabal_waiting"] = self.waiting
            return Task(
                id=task.id,
                contextId=task.context_id,
                status=TaskStatus(
                    state="input-required",
                    message=Message(role="agent", parts=[TextPart(text="committed; waiting for the build window")]),
                ),
                history=history,
                metadata=metadata,
            )
        return Task(
            id=task.id,
            contextId=task.context_id,
            status=TaskStatus(state="completed", message=Message(role="agent", parts=[TextPart(text="built")])),
            history=history,
            metadata=task.metadata,
        )


async def _send(client: AsyncClient, token: str) -> dict[str, Any]:
    body = (
        await client.post(
            "/agents/echo/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {
                    "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "port it"}]},
                    "configuration": {"returnImmediately": True},
                },
            },
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
        )
    ).json()
    assert "result" in body, body
    return body["result"]["task"]


async def _rows(app: FastAPI) -> dict[str, Conversation]:
    async with app.state.sessionmaker() as s:
        return {r.task_id: r for r in (await s.execute(select(Conversation))).scalars()}


async def _until_ended(app: FastAPI, task_id: str) -> Conversation:
    for _ in range(200):
        row = (await _rows(app)).get(task_id)
        if row is not None and row.state not in ("submitted", "working", "scheduled"):
            return row
        await asyncio.sleep(0.02)
    pytest.fail(f"{task_id} never ended")


def _reason(row: Conversation) -> str:
    return str(row.result_json["metadata"].get("cabal_reason_message", ""))


def test_705_is_input_required_on_the_wire() -> None:
    assert make_status(CabalReason(CODE_WAITING, "waiting")).state == "input-required"


@pytest.mark.asyncio
async def test_waiting_until_a_time_is_continued_in_the_same_conversation(
    app: FastAPI, client: AsyncClient
) -> None:
    until = datetime.now(UTC) + UNTIL
    agent = _Waiter({"condition": "the 17:00 build window", "until": until.isoformat()})
    app.state.dispatch_router.register_local(agent)
    token = await _token(app)
    first = await _send(client, token)
    row = await _until_ended(app, first["id"])
    assert row.state == "completed"
    meta = row.result_json["metadata"]
    assert meta["cabal_reason_code"] == CODE_WAITING
    nxt = meta["cabal_continued_by"]

    cont = (await _rows(app))[nxt]
    assert cont.state == "scheduled" and cont.caller_agent == "agent:alice"
    assert cont.schedule_json["sub"] == "agent:alice" and token not in str(cont.schedule_json)
    assert cont.result_json["contextId"] == first["contextId"]
    assert cont.result_json["metadata"]["cabal_continues"] == first["id"]
    assert cont.result_json["metadata"]["cabal_wait_count"] == 1

    assert await app.state.scheduler.tick(now=until) == 1
    done = await _until_ended(app, nxt)
    assert done.state == "completed"
    second = agent.seen[1]
    assert "the 17:00 build window" in second.status.message.parts[0].text
    said = [p.text for m in second.history for p in m.parts]
    assert "port it" in said and "committed; waiting for the build window" in said

    wire = (
        await client.post(
            "/agents/echo/a2a",
            json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": first["id"]}},
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
        )
    ).json()["result"]
    assert wire["status"]["state"] == "TASK_STATE_COMPLETED"
    assert wire["metadata"]["cabalContinuedBy"] == nxt
    assert wire["metadata"]["cabalWaiting"]["condition"] == "the 17:00 build window"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waiting", "why"),
    [
        ({"condition": "PR 407 merged"}, ""),
        ({"condition": "x", "until": "after lunch"}, "not an ISO 8601 time"),
        ({"condition": "x", "until": "2026-11-10T17:00:00"}, "no UTC offset"),
        (None, ""),
    ],
)
async def test_waiting_without_a_usable_time_pauses_for_the_caller(
    app: FastAPI, client: AsyncClient, waiting: dict[str, Any] | None, why: str
) -> None:
    app.state.dispatch_router.register_local(_Waiter(waiting))
    first = await _send(client, await _token(app))
    row = await _until_ended(app, first["id"])
    assert row.state == "input-required"
    assert "cabal_continued_by" not in row.result_json["metadata"]
    assert why in _reason(row)
    assert list(await _rows(app)) == [first["id"]]


@pytest.mark.asyncio
async def test_waiting_past_the_callers_token_pauses(app: FastAPI, client: AsyncClient) -> None:
    until = datetime.now(UTC) + timedelta(days=2)
    app.state.dispatch_router.register_local(_Waiter({"condition": "x", "until": until.isoformat()}))
    first = await _send(client, await _token(app))
    row = await _until_ended(app, first["id"])
    assert row.state == "input-required" and "expires" in _reason(row)


@pytest.mark.asyncio
async def test_the_repeat_cap_hands_it_back_to_the_caller(app: FastAPI, client: AsyncClient) -> None:
    router = app.state.dispatch_router
    router._max_wait_continuations = 1
    until = datetime.now(UTC) + UNTIL
    router.register_local(_Waiter({"condition": "x", "until": until.isoformat()}, waits=5))
    first = await _send(client, await _token(app))
    row = await _until_ended(app, first["id"])
    nxt = row.result_json["metadata"]["cabal_continued_by"]
    assert await app.state.scheduler.tick(now=until) == 1
    again = await _until_ended(app, nxt)
    assert again.state == "input-required" and "max_wait_continuations" in _reason(again)
    assert len(await _rows(app)) == 2


@pytest.mark.asyncio
async def test_a_door_without_the_callers_claims_pauses(app: FastAPI, client: AsyncClient) -> None:
    async with app.state.sessionmaker() as s:
        code = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    op = (await client.post("/auth/bootstrap", json={"code": code.code})).json()["access_token"]
    until = datetime.now(UTC) + UNTIL
    app.state.dispatch_router.register_local(_Waiter({"condition": "x", "until": until.isoformat()}))
    r = await client.post(
        "/a2a/tasks",
        json={
            "target": "echo",
            "task": {
                "id": "t-legacy",
                "status": {"state": "submitted", "message": {"role": "user", "parts": [{"kind": "text", "text": "x"}]}},
            },
        },
        headers={"Authorization": f"Bearer {op}"},
    )
    assert r.status_code == 200, r.text
    row = (await _rows(app))["t-legacy"]
    assert row.state == "input-required" and "claims" in _reason(row)
