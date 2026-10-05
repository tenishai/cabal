"""SendMessage content types and historyLength on the A2A v1.0 path (#274)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.a2a.v1_card import INPUT_MODES
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session


async def _token(app: FastAPI) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub="agent:alice", aud="cabal-core", scope=["colleague"])


async def _call(client: AsyncClient, token: str, method: str, params: Any) -> Response:
    return await client.post(
        "/agents/echo/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )


def _send_params(*parts: dict[str, Any], **configuration: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": list(parts)}
    }
    if configuration:
        params["configuration"] = configuration
    return params


# ---- content types (CORE-SEND-003) ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "part",
    [
        {"text": "hi"},
        {"text": "hi", "mediaType": "text/plain"},
        {"text": "hi", "mediaType": "text/plain; charset=utf-8"},
        {"text": "hi", "mediaType": "TEXT/PLAIN"},
        {"data": {"k": 1}},
        {"data": {"k": 1}, "mediaType": "application/json"},
    ],
)
async def test_a_declared_media_type_is_accepted(app: FastAPI, client: AsyncClient, part) -> None:
    r = await _call(client, await _token(app), "SendMessage", _send_params(part))
    body = r.json()
    assert "error" not in body, body
    assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts",
    [
        [{"text": "# hi", "mediaType": "text/markdown"}],
        [{"data": {"k": 1}, "mediaType": "application/xml"}],
        [{"text": "fine"}, {"text": "<p>no</p>", "mediaType": "text/html"}],
    ],
)
async def test_an_undeclared_media_type_is_refused(app: FastAPI, client: AsyncClient, parts) -> None:
    r = await _call(client, await _token(app), "SendMessage", _send_params(*parts))
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == -32005
    assert "is not accepted" in err["message"]


@pytest.mark.asyncio
async def test_the_served_card_declares_the_checked_input_modes(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setenv("CABAL_PUBLIC_URL", "https://cabal.test")
    card = (await client.get("/agents/echo/.well-known/agent-card.json")).json()
    assert card["defaultInputModes"] == list(INPUT_MODES)


# ---- historyLength (CORE-HIST-002) ---------------------------------------------


class _HistoryEcho:
    """Stands in for echo and returns a task with three history messages."""

    name = "echo"

    async def handle_task(self, task: Task) -> Task:
        history = [
            Message(role="user" if i % 2 == 0 else "agent", parts=[TextPart(text=f"h{i}")])
            for i in range(3)
        ]
        return Task(
            id=task.id,
            status=TaskStatus(
                state="completed", message=Message(role="agent", parts=[TextPart(text="done")])
            ),
            history=history,
            metadata=task.metadata,
        )


def _history_texts(task: dict[str, Any]) -> list[str]:
    return [m["parts"][0]["text"] for m in task.get("history", [])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "length, expected",
    [(None, ["h0", "h1", "h2"]), (0, []), (2, ["h1", "h2"]), (10, ["h0", "h1", "h2"])],
)
async def test_history_length_on_send_message_and_get_task(
    app: FastAPI, client: AsyncClient, length, expected
) -> None:
    app.state.dispatch_router.register_local(_HistoryEcho())
    token = await _token(app)
    config = {} if length is None else {"historyLength": length}

    sent = (await _call(client, token, "SendMessage", _send_params({"text": "hi"}, **config))).json()
    task = sent["result"]["task"]
    assert _history_texts(task) == expected
    if length == 0:
        assert "history" not in task

    get_params: dict[str, Any] = {"id": task["id"]}
    if length is not None:
        get_params["historyLength"] = length
    got = (await _call(client, token, "GetTask", get_params)).json()["result"]
    assert _history_texts(got) == expected
