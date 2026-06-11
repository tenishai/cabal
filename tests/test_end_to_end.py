"""End-to-end operator loop test.

Spin up the FastAPI app, fetch the bootstrap code via a side channel
(here: query the DB directly, since logs are hard to scrape in-process),
trade it for an operator JWT at POST /auth/bootstrap, dispatch a Task
through POST /a2a/tasks to the built-in echo agent, verify the response.

This is the smallest pipe that proves: AgentCard kid wiring, JWKS
exposure, JWT mint+verify, bulletin lookup, router dispatch to a local
handler, bookkeeping writes — all on real wire calls.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.models import BootstrapCode, Conversation, Event


async def _peek_bootstrap_code(app: FastAPI) -> str:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as s:
        result = await s.execute(
            select(BootstrapCode)
            .where(BootstrapCode.used_at.is_(None))
            .order_by(BootstrapCode.expires_at.desc())
            .limit(1)
        )
        row = result.scalar_one()
        return row.code


@pytest.mark.asyncio
async def test_operator_can_dispatch_echo_task_end_to_end(
    app: FastAPI, client: AsyncClient
) -> None:
    code = await _peek_bootstrap_code(app)
    resp = await client.post("/auth/bootstrap", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    assert token

    # Operator JWT has aud=cabal-core, but dispatching to a target uses the
    # target's aud. For Phase 1 we mint a SECOND short-lived token aimed at
    # echo; we'll wire the auth route to do this for the operator in Phase 2.
    # For now: hit the dispatch with an operator-scoped token aimed at echo.
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    assert stored is not None
    op_dispatch_token = mint(
        signing_key=stored, sub="operator:dimartin", aud="echo", scope=["operator"]
    )

    body = {
        "target": "echo",
        "task": {
            "id": "t-100",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello cabal"}],
                },
            },
        },
    }
    resp = await client.post(
        "/a2a/tasks",
        json=body,
        headers={"Authorization": f"Bearer {op_dispatch_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "t-100"
    assert body["status"]["state"] == "completed"
    text_part = body["status"]["message"]["parts"][0]
    assert text_part["text"] == "echo: hello cabal"

    # Bookkeeping happened
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as s:
        conv = (await s.execute(select(Conversation))).scalar_one()
        assert conv.task_id == "t-100"
        assert conv.state == "completed"
        events = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
        assert "dispatch_start" in [e.event_type for e in events]
        assert "dispatch_success" in [e.event_type for e in events]


@pytest.mark.asyncio
async def test_bootstrap_rejects_bad_code(client: AsyncClient) -> None:
    resp = await client.post("/auth/bootstrap", json={"code": "BADCD1"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bootstrap_double_use_rejected(app: FastAPI, client: AsyncClient) -> None:
    code = await _peek_bootstrap_code(app)
    r1 = await client.post("/auth/bootstrap", json={"code": code})
    assert r1.status_code == 200
    r2 = await client.post("/auth/bootstrap", json={"code": code})
    assert r2.status_code == 401
    assert "already used" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_tasks_endpoint_requires_bearer(client: AsyncClient) -> None:
    resp = await client.post("/a2a/tasks", json={"target": "echo", "task": {}})
    assert resp.status_code in (401, 422)  # missing auth or missing fields


@pytest.mark.asyncio
async def test_tasks_endpoint_rejects_invalid_token(client: AsyncClient) -> None:
    body = {
        "target": "echo",
        "task": {
            "id": "t-200",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hi"}],
                },
            },
        },
    }
    resp = await client.post(
        "/a2a/tasks", json=body, headers={"Authorization": "Bearer not.a.real.jwt"}
    )
    assert resp.status_code == 401
