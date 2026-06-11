"""A2A v1.0 JSON-RPC envelope tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.a2a.jsonrpc import (
    ERROR_AGENT_NOT_FOUND,
    ERROR_INVALID_REQUEST,
    ERROR_METHOD_NOT_FOUND,
    ERROR_UNAUTHORIZED,
)
from cabal.core.models import BootstrapCode


async def _bootstrap_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_well_known_agent_card_per_agent(app: FastAPI, client: AsyncClient) -> None:
    r = await client.get("/agents/echo/.well-known/agent-card")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "echo"


@pytest.mark.asyncio
async def test_well_known_unknown_agent(app: FastAPI, client: AsyncClient) -> None:
    r = await client.get("/agents/nope/.well-known/agent-card")
    assert r.status_code == 200
    assert r.json()["error"] == "agent not found"


@pytest.mark.asyncio
async def test_message_send_dispatches_to_echo(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _bootstrap_token(app, client)
    envelope = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "hello rpc"}],
            }
        },
    }
    r = await client.post(
        "/agents/echo",
        json=envelope,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "req-1"
    assert body.get("error") is None, body
    task = body["result"]
    assert task["status"]["state"] == "completed"
    assert task["status"]["message"]["parts"][0]["text"] == "echo: hello rpc"


@pytest.mark.asyncio
async def test_message_send_unknown_agent(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _bootstrap_token(app, client)
    envelope = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "message/send",
        "params": {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "x"}]}
        },
    }
    r = await client.post(
        "/agents/ghostly",
        json=envelope,
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    assert body["error"]["code"] == ERROR_AGENT_NOT_FOUND


@pytest.mark.asyncio
async def test_tasks_get_returns_persisted_result(
    app: FastAPI, client: AsyncClient
) -> None:
    """A message/send write + tasks/get read roundtrip."""
    token = await _bootstrap_token(app, client)
    send_env = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "store me"}]}
        },
    }
    r = await client.post(
        "/agents/echo",
        json=send_env,
        headers={"Authorization": f"Bearer {token}"},
    )
    task = r.json()["result"]
    task_id = task["id"]

    # Same agent path; tasks/get pulls the persisted result_json
    get_env = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tasks/get",
        "params": {"id": task_id},
    }
    r = await client.post(
        "/agents/echo",
        json=get_env,
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    assert body["result"]["id"] == task_id
    assert body["result"]["status"]["state"] == "completed"


@pytest.mark.asyncio
async def test_invalid_json_body_rejected(client: AsyncClient) -> None:
    r = await client.post(
        "/agents/echo",
        content="not json",
        headers={"Authorization": "Bearer abc.def.ghi", "Content-Type": "application/json"},
    )
    body = r.json()
    assert body["error"]["code"] == ERROR_INVALID_REQUEST


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _bootstrap_token(app, client)
    envelope = {"jsonrpc": "2.0", "id": 1, "method": "agents/banish", "params": {}}
    r = await client.post(
        "/agents/echo",
        json=envelope,
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    assert body["error"]["code"] == ERROR_METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_missing_auth_returns_unauthorized(client: AsyncClient) -> None:
    envelope = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}
    r = await client.post("/agents/echo", json=envelope)
    body = r.json()
    assert body["error"]["code"] == ERROR_UNAUTHORIZED


@pytest.mark.asyncio
async def test_tasks_get_unknown_id(app: FastAPI, client: AsyncClient) -> None:
    token = await _bootstrap_token(app, client)
    envelope = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tasks/get",
        "params": {"id": "never-existed"},
    }
    r = await client.post(
        "/agents/echo",
        json=envelope,
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    assert body["error"]["code"] == ERROR_AGENT_NOT_FOUND
    assert "never-existed" in body["error"]["message"]
