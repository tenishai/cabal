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
    # Reading tasks checks the token's audience too (#296): on another
    # persona's path every task method is refused, as SendMessage is.
    sent = (await _call(client, token, "SendMessage", _SEND)).json()["result"]["task"]
    for method, params in (("GetTask", {"id": sent["id"]}), ("ListTasks", {}), ("CancelTask", {"id": sent["id"]})):
        other = await _call(client, token, method, params, agent="librarian")
        assert other.status_code == 403, (method, other.text)
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


_TASK_METHODS = ("GetTask", "ListTasks", "CancelTask")


def _params(method: str, task_id: str) -> dict[str, Any]:
    return {} if method == "ListTasks" else {"id": task_id}


async def _paused_echo_task(app: FastAPI, task_id: str) -> None:
    """An `input-required` task the outsider sent to echo, so CancelTask and
    an answer have something unfinished to act on."""
    from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
    from cabal.core.bulletin import store as bulletin_store
    from cabal.core.models import Conversation

    async with app.state.sessionmaker() as s:
        echo = await bulletin_store.get_active(s, "echo")
        assert echo is not None
        paused = Task(
            id=task_id,
            contextId="ctx-296",
            status=TaskStatus(
                state="input-required",
                message=Message(role="agent", parts=[TextPart(text="which one?")]),
            ),
            history=[Message(role="user", parts=[TextPart(text="do it")])],
        )
        s.add(
            Conversation(
                task_id=task_id,
                target_agent="echo",
                target_generation=echo.generation,
                caller_agent=OUTSIDER,
                state="input-required",
                result_json=paused.model_dump(by_alias=True, exclude_none=True),
            )
        )
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _TASK_METHODS)
async def test_a_token_for_the_paths_persona_reads_its_tasks(
    app: FastAPI, client: AsyncClient, method: str
) -> None:
    token = await _outside_token(client, await _operator_token(app, client), "echo")
    await _paused_echo_task(app, "t-296-own")
    r = await _call(client, token, method, _params(method, "t-296-own"))
    assert r.status_code == 200, r.text
    assert "result" in r.json(), r.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", (*_TASK_METHODS, "answer"))
async def test_a_token_for_another_persona_is_refused_on_this_path(
    app: FastAPI, client: AsyncClient, method: str
) -> None:
    # #296: before, any valid token passed on any persona's path, and only
    # the `sub` filter stood between it and the tasks.
    token = await _outside_token(client, await _operator_token(app, client), "librarian")
    await _paused_echo_task(app, "t-296-other")
    if method == "answer":
        message = {"messageId": "m-a", "taskId": "t-296-other", "role": "ROLE_USER", "parts": [{"text": "yes"}]}
        r = await _call(client, token, "SendMessage", {"message": message})
    else:
        r = await _call(client, token, method, _params(method, "t-296-other"))
    assert r.status_code == 403, r.text
    assert "cannot read tasks on 'echo'" in r.text
    async with open_session(app.state.sessionmaker) as session:
        from cabal.core.models import Conversation

        row = (await session.execute(select(Conversation).where(Conversation.task_id == "t-296-other"))).scalar_one()
    assert row.state == "input-required"  # nothing was canceled or answered


@pytest.mark.asyncio
async def test_a_dispatch_token_for_cabal_core_is_refused(app: FastAPI, client: AsyncClient) -> None:
    # `cabal-core` is the operator's and colleagues' audience; a plain
    # dispatch-scope token aimed there names no persona.
    r = await client.post(
        "/admin/token/mint-service",
        json={"sub": OUTSIDER, "scope": ["dispatch"], "aud": "cabal-core", "ttl_days": 1},
        headers={"Authorization": f"Bearer {await _operator_token(app, client)}"},
    )
    token = r.json()["access_token"]
    await _paused_echo_task(app, "t-296-core")
    for method in _TASK_METHODS:
        got = await _call(client, token, method, _params(method, "t-296-core"))
        assert got.status_code == 403, (method, got.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _TASK_METHODS)
async def test_the_operator_still_reads_every_persona(
    app: FastAPI, client: AsyncClient, method: str
) -> None:
    operator = await _operator_token(app, client)
    await _paused_echo_task(app, "t-296-op")
    r = await _call(client, operator, method, _params(method, "t-296-op"))
    assert r.status_code == 200, r.text
    assert "result" in r.json(), r.json()
    if method == "ListTasks":
        assert "t-296-op" in [t["id"] for t in r.json()["result"]["tasks"]]
