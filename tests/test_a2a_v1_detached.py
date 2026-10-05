"""returnImmediately: detached dispatch, its owner, the cap, the restart sweep (#277)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.abort import AbortCoordinator, TaskAborted
from cabal.core.app import create_app
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.detached import DetachedTasks
from cabal.core.models import Conversation


async def _token(app: FastAPI, scope: list[str] | None = None) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub="agent:alice", aud="cabal-core", scope=scope or ["colleague"])


async def _call(client: AsyncClient, token: str, method: str, params: Any) -> Response:
    return await client.post(
        "/agents/echo/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )


def _send_params(text: str = "work") -> dict[str, Any]:
    return {
        "message": {"messageId": f"m-{text}", "role": "ROLE_USER", "parts": [{"text": text}]},
        "configuration": {"returnImmediately": True},
    }


async def _state(client: AsyncClient, token: str, task_id: str) -> str:
    return (await _call(client, token, "GetTask", {"id": task_id})).json()["result"]["status"]["state"]


async def _until(client: AsyncClient, token: str, task_id: str, state: str) -> None:
    for _ in range(200):
        if await _state(client, token, task_id) == state:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"task {task_id} never reached {state}")


class _Gated:
    """Stands in for echo and waits for `release`, cooperatively, so a test
    can look at the task while it runs."""

    name = "echo"

    def __init__(self, app: FastAPI) -> None:
        self._abort = app.state.dispatch_router.abort_coordinator
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_task(self, task: Task) -> Task:
        self.started.set()
        await self._abort.guard(task.id, self.release.wait())
        assert task.status.message is not None
        return Task(
            id=task.id,
            contextId=task.context_id,
            status=TaskStatus(
                state="completed", message=Message(role="agent", parts=[TextPart(text="done")])
            ),
            history=[*task.history, task.status.message],
            metadata=task.metadata,
        )


@pytest.mark.asyncio
async def test_return_immediately_answers_at_once_then_runs(
    app: FastAPI, client: AsyncClient
) -> None:
    gated = _Gated(app)
    app.state.dispatch_router.register_local(gated)
    token = await _token(app)

    body = (await _call(client, token, "SendMessage", _send_params())).json()
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert task["contextId"]

    await asyncio.wait_for(gated.started.wait(), timeout=5)
    await _until(client, token, task["id"], "TASK_STATE_WORKING")
    gated.release.set()
    await _until(client, token, task["id"], "TASK_STATE_COMPLETED")
    final = (await _call(client, token, "GetTask", {"id": task["id"]})).json()["result"]
    assert final["status"]["message"]["parts"] == [{"text": "done"}]
    assert len(app.state.detached) == 0


@pytest.mark.asyncio
async def test_cancel_task_reaches_a_detached_task(app: FastAPI, client: AsyncClient) -> None:
    gated = _Gated(app)
    app.state.dispatch_router.register_local(gated)
    token = await _token(app)
    task = (await _call(client, token, "SendMessage", _send_params())).json()["result"]["task"]
    await asyncio.wait_for(gated.started.wait(), timeout=5)

    canceled = (await _call(client, token, "CancelTask", {"id": task["id"]})).json()["result"]
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_the_cap_refuses_and_frees_its_slots(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(
        app.state,
        "detached",
        DetachedTasks(limit=1, abort=app.state.dispatch_router.abort_coordinator),
    )
    gated = _Gated(app)
    app.state.dispatch_router.register_local(gated)
    token = await _token(app)

    first = (await _call(client, token, "SendMessage", _send_params("one"))).json()["result"]["task"]
    over = (await _call(client, token, "SendMessage", _send_params("two"))).json()
    assert over["error"]["code"] == -32004
    assert "too many tasks are running detached" in over["error"]["message"]

    gated.release.set()
    await _until(client, token, first["id"], "TASK_STATE_COMPLETED")
    again = (await _call(client, token, "SendMessage", _send_params("three"))).json()
    assert "result" in again, again


@pytest.mark.asyncio
async def test_a_refused_start_gives_its_slot_back(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """The slot is reserved before the start phase; a start that fails there
    (here, an unknown persona: AgentNotFoundError, HTTP 404) gives it back."""
    detached = DetachedTasks(limit=1, abort=app.state.dispatch_router.abort_coordinator)
    monkeypatch.setattr(app.state, "detached", detached)
    token = await _token(app)
    r = await client.post(
        "/agents/ghostly/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": _send_params()},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
    assert detached.try_reserve() is True  # the one slot is free again


@pytest.mark.asyncio
async def test_without_the_flag_send_message_still_blocks(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _token(app)
    params = _send_params()
    del params["configuration"]
    task = (await _call(client, token, "SendMessage", params)).json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"


# ---- restart and shutdown --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_fails_tasks_left_running(app: FastAPI, client: AsyncClient) -> None:
    async with app.state.sessionmaker() as s:
        echo = await bulletin_store.get_active(s, "echo")
        assert echo is not None
        for task_id, state in (("t-queued", "submitted"), ("t-running", "working")):
            s.add(
                Conversation(
                    task_id=task_id,
                    target_agent="echo",
                    target_generation=echo.generation,
                    caller_agent="agent:alice",
                    state=state,
                )
            )
        await s.commit()

    # A second start on the same database runs the startup sweep.
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    restarted = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    async with restarted.router.lifespan_context(restarted):
        pass

    token = await _token(app)
    for task_id in ("t-queued", "t-running"):
        got = (await _call(client, token, "GetTask", {"id": task_id})).json()["result"]
        assert got["status"]["state"] == "TASK_STATE_FAILED"
        assert got["status"]["message"]["parts"] == [{"text": "interrupted by a restart"}]


@pytest.mark.asyncio
async def test_drain_stops_runs_that_listen_and_cancels_the_rest() -> None:
    abort = AbortCoordinator()
    detached = DetachedTasks(limit=4, abort=abort)
    outcomes: dict[str, str] = {}

    async def listens(task_id: str) -> None:
        abort.register(task_id)
        try:
            await abort.guard(task_id, asyncio.sleep(30))
        except TaskAborted:
            outcomes[task_id] = "stopped"

    async def ignores(task_id: str) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcomes[task_id] = "cancelled"
            raise

    for task_id, run in (("a", listens("a")), ("b", ignores("b"))):
        assert detached.try_reserve()
        detached.spawn(task_id, run)
    await asyncio.sleep(0)
    await detached.drain(grace_seconds=0.2)
    await asyncio.sleep(0)
    assert outcomes == {"a": "stopped", "b": "cancelled"}
    assert len(detached) == 0
