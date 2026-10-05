"""SendMessage and GetTask on the A2A v1.0 path, against the echo agent (#267).

The v1.0 answers are compared with the legacy `message/send` and `tasks/get`
on `POST /agents/echo`, so the two interfaces cannot drift apart unnoticed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode

V1_PATH = "/agents/echo/a2a"
V1 = {"A2A-Version": "1.0"}


async def _bootstrap_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _colleague_token(app: FastAPI, sub: str, scope: list[str] | None = None) -> str:
    """A valid non-operator token, colleague scope unless told otherwise."""
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud="cabal-core", scope=scope or ["colleague"])


async def _stranger_token(app: FastAPI) -> str:
    """A valid token for a caller that is neither the operator nor the task's owner."""
    return await _colleague_token(app, "agent:stranger")


def _message(text: str = "hello v1", **extra: Any) -> dict[str, Any]:
    return {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": text}], **extra}


async def _call(
    client: AsyncClient,
    token: str | None,
    method: str,
    params: Any,
    *,
    path: str = V1_PATH,
    request_id: Any = 7,
) -> Response:
    headers = dict(V1)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    envelope = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    return await client.post(path, json=envelope, headers=headers)


def _error_code(r: Response) -> int:
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["id"] == 7
    assert "result" not in body
    return body["error"]["code"]


async def _legacy(client: AsyncClient, token: str, method: str, params: dict) -> dict:
    envelope = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = await client.post("/agents/echo", json=envelope, headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert body.get("error") is None, body
    return body["result"]


# ---- SendMessage -------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_matches_legacy_message_send(app: FastAPI, client: AsyncClient) -> None:
    token = await _bootstrap_token(app, client)
    legacy = await _legacy(
        client,
        token,
        "message/send",
        {"message": {"role": "user", "parts": [{"kind": "text", "text": "hello v1"}]}},
    )

    r = await _call(client, token, "SendMessage", {"message": _message()})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["id"] == 7
    assert "error" not in body, body
    task = body["result"]["task"]

    assert legacy["status"]["state"] == "completed"
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    legacy_text = legacy["status"]["message"]["parts"][0]["text"]
    assert task["status"]["message"]["parts"][0]["text"] == legacy_text == "echo: hello v1"
    assert task["status"]["message"]["role"] == "ROLE_AGENT"
    assert task["status"]["timestamp"]
    assert task["id"] != legacy["id"]


@pytest.mark.asyncio
async def test_send_message_accepts_the_matching_tenant(app: FastAPI, client: AsyncClient) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, "SendMessage", {"tenant": "echo", "message": _message()})
    assert r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_send_message_refuses_another_tenant(app: FastAPI, client: AsyncClient) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, "SendMessage", {"tenant": "librarian", "message": _message()})
    assert _error_code(r) == -32602


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        [],
        {},
        {"message": {"role": "ROLE_USER", "parts": [{"text": "no id"}]}},
        {"message": _message(role="user")},
        {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": []}},
        {"message": _message(), "configuration": {"historyLength": -1}},
    ],
)
async def test_send_message_malformed_params(app: FastAPI, client: AsyncClient, params) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, "SendMessage", params)
    assert _error_code(r) == -32602


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params, code",
    [
        ({"message": _message(), "configuration": {"taskPushNotificationConfig": {"url": "x"}}}, -32003),
        ({"message": _message(), "configuration": {"returnImmediately": True}}, -32004),
        ({"message": _message(taskId="t-earlier")}, -32004),
        (
            {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"url": "https://x"}]}},
            -32005,
        ),
    ],
)
async def test_send_message_unsupported_features(
    app: FastAPI, client: AsyncClient, params, code
) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, "SendMessage", params)
    assert _error_code(r) == code


@pytest.mark.asyncio
async def test_send_message_to_an_unknown_agent_is_404(app: FastAPI, client: AsyncClient) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, "SendMessage", {"message": _message()}, path="/agents/ghostly/a2a")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_token_without_operator_or_colleague_scope_is_403(app: FastAPI, client: AsyncClient) -> None:
    token = await _colleague_token(app, "agent:narrow", scope=["dispatch"])
    r = await _call(client, token, "SendMessage", {"message": _message()})
    assert r.status_code == 403
    assert "requires operator or colleague scope" in r.text


@pytest.mark.asyncio
async def test_an_invalid_token_is_401(client: AsyncClient) -> None:
    r = await _call(client, "not-a-jwt", "SendMessage", {"message": _message()})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


# ---- GetTask -----------------------------------------------------------------


async def _sent_task(client: AsyncClient, token: str) -> dict:
    r = await _call(client, token, "SendMessage", {"message": _message("store me")})
    return r.json()["result"]["task"]


@pytest.mark.asyncio
async def test_get_task_matches_legacy_tasks_get(app: FastAPI, client: AsyncClient) -> None:
    token = await _bootstrap_token(app, client)
    sent = await _sent_task(client, token)

    legacy = await _legacy(client, token, "tasks/get", {"id": sent["id"]})
    r = await _call(client, token, "GetTask", {"id": sent["id"]})
    assert r.headers["content-type"].startswith("application/json")
    task = r.json()["result"]

    assert task["id"] == legacy["id"] == sent["id"]
    assert legacy["status"]["state"] == "completed"
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["parts"][0]["text"] == "echo: store me"
    assert task["status"]["message"] == sent["status"]["message"]


@pytest.mark.asyncio
async def test_a_non_operator_caller_reads_its_own_task(app: FastAPI, client: AsyncClient) -> None:
    """The owner branch of GetTask: the main audience of this endpoint is a
    non-operator A2A client reading back the task it sent."""
    owner = await _colleague_token(app, "agent:owner")
    sent = await _sent_task(client, owner)
    r = await _call(client, owner, "GetTask", {"id": sent["id"]})
    body = r.json()
    assert "error" not in body, body
    assert body["result"]["id"] == sent["id"]
    assert body["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_get_task_history_length_zero_drops_history(app: FastAPI, client: AsyncClient) -> None:
    token = await _bootstrap_token(app, client)
    sent = await _sent_task(client, token)
    r = await _call(client, token, "GetTask", {"id": sent["id"], "historyLength": 0})
    assert "history" not in r.json()["result"]


@pytest.mark.asyncio
async def test_get_task_hides_whether_a_task_exists(app: FastAPI, client: AsyncClient) -> None:
    """Missing, another caller's, and another persona's task get one answer."""
    token = await _bootstrap_token(app, client)
    sent = await _sent_task(client, token)
    stranger = await _stranger_token(app)

    missing = await _call(client, token, "GetTask", {"id": "no-such-task"})
    not_yours = await _call(client, stranger, "GetTask", {"id": sent["id"]})
    other_persona = await _call(
        client, token, "GetTask", {"id": sent["id"]}, path="/agents/librarian/a2a"
    )
    answers = [r.json()["error"] for r in (missing, not_yours, other_persona)]
    assert all(_error_code(r) == -32001 for r in (missing, not_yours, other_persona))
    assert answers[0] == answers[1] == answers[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [[], {}, {"id": "t", "historyLength": -1}, {"id": "t", "tenant": "librarian"}],
)
async def test_get_task_malformed_params(app: FastAPI, client: AsyncClient, params) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, "GetTask", params)
    assert _error_code(r) == -32602


# ---- other methods -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["SendStreamingMessage", "SubscribeToTask"])
async def test_streaming_methods_are_unsupported(app: FastAPI, client: AsyncClient, method) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, method, {"message": _message()})
    assert _error_code(r) == -32004


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["message/send", "tasks/get", "NoSuchMethod"])
async def test_other_methods_are_not_found(app: FastAPI, client: AsyncClient, method) -> None:
    token = await _bootstrap_token(app, client)
    r = await _call(client, token, method, {})
    assert _error_code(r) == -32601
