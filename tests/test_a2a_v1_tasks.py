"""ListTasks, CancelTask and the declined operations on the A2A v1.0 path (#268)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode, Conversation, Persona

V1 = {"A2A-Version": "1.0"}


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _token(app: FastAPI, sub: str | None, scope: list[str] | None = None) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud="cabal-core", scope=scope or ["colleague"])  # type: ignore[arg-type]


async def _call(
    client: AsyncClient, token: str, method: str, params: Any, *, agent: str = "echo"
) -> Response:
    return await client.post(
        f"/agents/{agent}/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        headers={**V1, "Authorization": f"Bearer {token}"},
    )


def _ok(r: Response) -> dict:
    body = r.json()
    assert "error" not in body, body
    return body["result"]


def _code(r: Response) -> int:
    assert r.status_code == 200, r.text
    return r.json()["error"]["code"]


async def _send(client: AsyncClient, token: str, text: str = "hi", **message: Any) -> dict:
    msg = {"messageId": f"m-{text}", "role": "ROLE_USER", "parts": [{"text": text}], **message}
    return _ok(await _call(client, token, "SendMessage", {"message": msg}))["task"]


async def _list(client: AsyncClient, token: str, agent: str = "echo", **params: Any) -> dict:
    return _ok(await _call(client, token, "ListTasks", params, agent=agent))


# ---- ListTasks -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_is_scoped_to_the_caller(app: FastAPI, client: AsyncClient) -> None:
    alice = await _token(app, "agent:alice")
    bob = await _token(app, "agent:bob")
    a1 = await _send(client, alice, "a1")
    a2 = await _send(client, alice, "a2")
    b1 = await _send(client, bob, "b1")

    assert {t["id"] for t in (await _list(client, alice))["tasks"]} == {a1["id"], a2["id"]}
    assert {t["id"] for t in (await _list(client, bob))["tasks"]} == {b1["id"]}
    operator = await _operator_token(app, client)
    listed = await _list(client, operator)
    assert {a1["id"], a2["id"], b1["id"]} <= {t["id"] for t in listed["tasks"]}


@pytest.mark.asyncio
async def test_list_tasks_is_scoped_to_the_persona(app: FastAPI, client: AsyncClient) -> None:
    operator = await _operator_token(app, client)
    await _send(client, operator)
    assert (await _list(client, operator, agent="librarian")) == {
        "tasks": [],
        "nextPageToken": "",
        "pageSize": 50,
        "totalSize": 0,
    }


@pytest.mark.asyncio
async def test_a_non_operator_token_without_sub_lists_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    operator = await _operator_token(app, client)
    await _send(client, operator)
    nobody = await _token(app, None)
    listed = await _list(client, nobody)
    assert listed["tasks"] == [] and listed["totalSize"] == 0


@pytest.mark.asyncio
async def test_list_tasks_newest_first_and_pages_to_an_empty_token(
    app: FastAPI, client: AsyncClient
) -> None:
    alice = await _token(app, "agent:alice")
    sent = [(await _send(client, alice, f"t{i}"))["id"] for i in range(5)]

    seen: list[str] = []
    token = ""
    pages = 0
    while True:
        params: dict[str, Any] = {"pageSize": 2}
        if token:
            params["pageToken"] = token
        page = await _list(client, alice, **params)
        assert page["pageSize"] == 2
        assert page["totalSize"] == 5
        assert "nextPageToken" in page
        seen += [t["id"] for t in page["tasks"]]
        pages += 1
        token = page["nextPageToken"]
        if token == "":
            break
    assert pages == 3
    assert seen == list(reversed(sent))


@pytest.mark.asyncio
async def test_list_tasks_omits_artifacts_unless_asked(app: FastAPI, client: AsyncClient) -> None:
    class ArtifactEcho:
        name = "echo"

        async def handle_task(self, task: Task) -> Task:
            return Task(
                id=task.id,
                status=TaskStatus(
                    state="completed",
                    message=Message(role="agent", parts=[TextPart(text="done")]),
                ),
                artifacts=[{"artifactId": "a-1", "parts": [{"kind": "text", "text": "report"}]}],
                metadata=task.metadata,
            )

    app.state.dispatch_router.register_local(ArtifactEcho())
    alice = await _token(app, "agent:alice")
    await _send(client, alice)

    (plain,) = (await _list(client, alice))["tasks"]
    assert "artifacts" not in plain
    (full,) = (await _list(client, alice, includeArtifacts=True))["tasks"]
    assert full["artifacts"][0]["artifactId"] == "a-1"
    assert full["artifacts"][0]["parts"] == [{"text": "report"}]


@pytest.mark.asyncio
async def test_include_artifacts_gives_an_empty_list_for_a_task_without_any(
    app: FastAPI, client: AsyncClient
) -> None:
    alice = await _token(app, "agent:alice")
    await _send(client, alice)
    (task,) = (await _list(client, alice, includeArtifacts=True))["tasks"]
    assert task["artifacts"] == []


class _ContextEcho:
    """Echo that keeps the task's contextId, as a persona does; the stock
    EchoAgent drops it."""

    name = "echo"

    async def handle_task(self, task: Task) -> Task:
        return Task(
            id=task.id,
            contextId=task.context_id,
            status=TaskStatus(
                state="completed", message=Message(role="agent", parts=[TextPart(text="ok")])
            ),
            metadata=task.metadata,
        )


@pytest.mark.asyncio
async def test_list_tasks_filters(app: FastAPI, client: AsyncClient) -> None:
    app.state.dispatch_router.register_local(_ContextEcho())
    alice = await _token(app, "agent:alice")
    in_ctx = await _send(client, alice, "a", contextId="ctx-1")
    await _send(client, alice, "b")

    by_ctx = await _list(client, alice, contextId="ctx-1")
    assert [t["id"] for t in by_ctx["tasks"]] == [in_ctx["id"]]
    assert by_ctx["totalSize"] == 1

    assert (await _list(client, alice, status="TASK_STATE_COMPLETED"))["totalSize"] == 2
    assert (await _list(client, alice, status="TASK_STATE_CANCELED"))["totalSize"] == 0
    assert (await _list(client, alice, status="TASK_STATE_UNSPECIFIED"))["totalSize"] == 2

    assert (await _list(client, alice, statusTimestampAfter="2000-01-01T00:00:00Z"))["totalSize"] == 2
    assert (await _list(client, alice, statusTimestampAfter="2999-01-01T00:00:00Z"))["totalSize"] == 0


@pytest.mark.asyncio
async def test_list_tasks_history_length(app: FastAPI, client: AsyncClient) -> None:
    alice = await _token(app, "agent:alice")
    await _send(client, alice)
    (task,) = (await _list(client, alice, historyLength=0))["tasks"]
    assert "history" not in task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        [],
        {"pageSize": 0},
        {"pageSize": 101},
        {"pageToken": "not-a-token"},
        {"status": "TASK_STATE_BOGUS"},
        {"historyLength": -1},
        {"tenant": "librarian"},
    ],
)
async def test_list_tasks_malformed_params(app: FastAPI, client: AsyncClient, params) -> None:
    alice = await _token(app, "agent:alice")
    assert _code(await _call(client, alice, "ListTasks", params)) == -32602


# ---- CancelTask -----------------------------------------------------------------


class _SlowEcho:
    """Stands in for echo and runs until canceled, cooperatively."""

    name = "echo"

    def __init__(self, app: FastAPI) -> None:
        self._abort = app.state.dispatch_router.abort_coordinator
        self.started = asyncio.Event()
        self.task_id: str | None = None

    async def handle_task(self, task: Task) -> Task:
        self.task_id = task.id
        self.started.set()
        await self._abort.guard(task.id, asyncio.sleep(30))
        raise AssertionError("the sleep was not canceled")  # pragma: no cover


@pytest.mark.asyncio
async def test_cancel_a_running_local_task(app: FastAPI, client: AsyncClient) -> None:
    slow = _SlowEcho(app)
    app.state.dispatch_router.register_local(slow)
    alice = await _token(app, "agent:alice")

    sending = asyncio.create_task(_call(client, alice, "SendMessage", {"message": {
        "messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "work"}]}}))
    await asyncio.wait_for(slow.started.wait(), timeout=5)
    assert slow.task_id is not None

    canceled = _ok(await _call(client, alice, "CancelTask", {"id": slow.task_id}))
    assert canceled["id"] == slow.task_id
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"

    sent = _ok(await asyncio.wait_for(sending, timeout=5))["task"]
    assert sent["status"]["state"] == "TASK_STATE_CANCELED"

    # A repeat finds the task canceled: TaskNotCancelableError (spec 3.1.5).
    assert _code(await _call(client, alice, "CancelTask", {"id": slow.task_id})) == -32002
    got = _ok(await _call(client, alice, "GetTask", {"id": slow.task_id}))
    assert got["status"]["state"] == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_cancel_a_finished_task(app: FastAPI, client: AsyncClient) -> None:
    alice = await _token(app, "agent:alice")
    task = await _send(client, alice)
    assert _code(await _call(client, alice, "CancelTask", {"id": task["id"]})) == -32002


@pytest.mark.asyncio
async def test_cancel_hides_whether_a_task_exists(app: FastAPI, client: AsyncClient) -> None:
    alice = await _token(app, "agent:alice")
    bob = await _token(app, "agent:bob")
    task = await _send(client, alice)
    assert _code(await _call(client, bob, "CancelTask", {"id": task["id"]})) == -32001
    assert _code(await _call(client, alice, "CancelTask", {"id": "no-such-task"})) == -32001
    assert _code(await _call(client, alice, "CancelTask", {"id": task["id"]}, agent="librarian")) == -32001


async def _in_flight_row(app: FastAPI, task_id: str, target: str, caller: str | None) -> None:
    async with app.state.sessionmaker() as s:
        row = await bulletin_store.get_active(s, target)
        assert row is not None
        s.add(
            Conversation(
                task_id=task_id,
                target_agent=target,
                target_generation=row.generation,
                caller_agent=caller,
                state="submitted",
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_cancel_a_task_on_a_federated_peer(app: FastAPI, client: AsyncClient) -> None:
    async with app.state.sessionmaker() as s:
        s.add(Persona(name="peer", config_json={}))
        await s.flush()
        await bulletin_store.register_federated(
            s,
            name="peer",
            persona_name="peer",
            endpoint="https://peer.example",
            federation_audience="peer@remote",
            agent_card_json={"name": "peer"},
            agent_card_kid="kid-peer",
        )
        await s.commit()
    await _in_flight_row(app, "t-remote", "peer", "agent:alice")
    alice = await _token(app, "agent:alice")
    r = await _call(client, alice, "CancelTask", {"id": "t-remote"}, agent="peer")
    assert _code(r) == -32002
    assert "outside this process" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_cancel_a_local_task_that_is_not_running(app: FastAPI, client: AsyncClient) -> None:
    """A row left in flight with nothing running behind it (say, after a restart)."""
    await _in_flight_row(app, "t-orphan", "echo", "agent:alice")
    alice = await _token(app, "agent:alice")
    r = await _call(client, alice, "CancelTask", {"id": "t-orphan"})
    assert _code(r) == -32002
    assert "not running" in r.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [[], {}, {"id": "t", "tenant": "librarian"}])
async def test_cancel_task_malformed_params(app: FastAPI, client: AsyncClient, params) -> None:
    alice = await _token(app, "agent:alice")
    assert _code(await _call(client, alice, "CancelTask", params)) == -32602


# ---- declined by capability ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, code",
    [
        ("CreateTaskPushNotificationConfig", -32003),
        ("GetTaskPushNotificationConfig", -32003),
        ("ListTaskPushNotificationConfigs", -32003),
        ("DeleteTaskPushNotificationConfig", -32003),
        ("GetExtendedAgentCard", -32004),
        ("SubscribeToTask", -32004),
    ],
)
async def test_declined_operations(app: FastAPI, client: AsyncClient, method, code) -> None:
    alice = await _token(app, "agent:alice")
    assert _code(await _call(client, alice, method, {})) == code
