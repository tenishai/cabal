"""An outside agent with per-persona tokens, as decided on #271.

The operator admits a named outside agent by minting it one token per persona
it may call, with `POST /admin/token/mint-service`: sub "agent:<name>", aud the
persona, scope ["dispatch"]. A token
already aimed at its target is used as it is (`_dispatch_auth.dispatch_token`),
so the room's dispatch-pairs table, which only knows the room's own agents,
does not come into it. These tests pin that path end to end.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select

from cabal.core import dispatch_pairs
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode

OUTSIDER = "agent:ext-partner"


async def _outside_token(client: AsyncClient, operator: str, persona: str) -> str:
    """Mint the outsider's token the way the operator does, through
    `POST /admin/token/mint-service` (what `cbl token mint-service` calls)."""
    r = await client.post(
        "/admin/token/mint-service",
        json={"sub": OUTSIDER, "scope": ["dispatch"], "aud": persona, "ttl_days": 1},
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    return (await client.post("/auth/bootstrap", json={"code": row.code})).json()["access_token"]


async def _call(
    client: AsyncClient, token: str, method: str, params: Any, *, agent: str = "echo"
) -> Response:
    return await client.post(
        f"/agents/{agent}/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )


_SEND = {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}}


@pytest.mark.asyncio
async def test_a_per_persona_token_works_whatever_the_rooms_pairs_say(
    app: FastAPI, client: AsyncClient
) -> None:
    # The room has synced a table that does not name the outsider at all.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", "echo")])
    token = await _outside_token(client, await _operator_token(app, client), "echo")

    sent = (await _call(client, token, "SendMessage", _SEND)).json()
    assert "result" in sent, sent
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"

    got = (await _call(client, token, "GetTask", {"id": task["id"]})).json()
    assert got["result"]["id"] == task["id"]
    listed = (await _call(client, token, "ListTasks", {})).json()["result"]
    assert [t["id"] for t in listed["tasks"]] == [task["id"]]


@pytest.mark.asyncio
async def test_a_per_persona_token_sends_only_to_its_persona(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _outside_token(client, await _operator_token(app, client), "echo")
    # Another persona: the token is not aimed at it, and dispatch scope
    # cannot ask for a re-mint.
    r = await _call(client, token, "SendMessage", _SEND, agent="librarian")
    assert r.status_code == 403
    assert "requires operator or colleague scope" in r.text
    # Reading tasks checks the caller, not the token's audience: on another
    # persona's path the outsider finds none of its echo tasks, and no 403.
    sent = (await _call(client, token, "SendMessage", _SEND)).json()["result"]["task"]
    other = await _call(client, token, "GetTask", {"id": sent["id"]}, agent="librarian")
    assert other.status_code == 200
    assert other.json()["error"]["code"] == -32001
    listed = (await _call(client, token, "ListTasks", {}, agent="librarian")).json()["result"]
    assert listed["tasks"] == []
    # The roster stays private: /directory wants a cabal-core token.
    d = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert d.status_code in (401, 403)


@pytest.mark.asyncio
async def test_revoking_the_outsider_stops_every_token_it_holds(
    app: FastAPI, client: AsyncClient
) -> None:
    operator = await _operator_token(app, client)
    token = await _outside_token(client, operator, "echo")
    second = await _outside_token(client, operator, "librarian")
    before = (await _call(client, token, "SendMessage", _SEND)).json()
    assert before["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    revoked = await client.post(
        "/admin/token/revoke",
        json={"sub": OUTSIDER},
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert revoked.status_code == 200, revoked.text

    r = await _call(client, token, "SendMessage", _SEND)
    assert r.status_code == 401
    # The token for the other persona stops too: revocation is per subject.
    r2 = await _call(client, second, "GetTask", {"id": "any"}, agent="librarian")
    assert r2.status_code == 401
