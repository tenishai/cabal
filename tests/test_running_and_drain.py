"""Running tasks and the drain (cabal issue 405)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select
from typer.testing import CliRunner

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.broker.router import DrainingError, TaskAlreadyRunning
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode, Conversation
from cabal.core.running import RunningTask, RunningTasks

# ---- helpers -------------------------------------------------------------------


async def _operator(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _token(app: FastAPI, sub: str, aud: str = "cabal-core") -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    scope = ["colleague"] if aud == "cabal-core" else ["dispatch"]
    return mint(signing_key=stored, sub=sub, aud=aud, scope=scope)


async def _send(
    client: AsyncClient, token: str, *, detach: bool = True, metadata: dict[str, Any] | None = None
) -> Response:
    params: dict[str, Any] = {
        "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "work"}]},
    }
    if detach:
        params["configuration"] = {"returnImmediately": True}
    if metadata:
        params["metadata"] = metadata
    return await client.post(
        "/agents/echo/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": params},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )


async def _running(client: AsyncClient, op: str) -> dict[str, Any]:
    r = await client.get("/admin/tasks/running", headers={"Authorization": f"Bearer {op}"})
    assert r.status_code == 200, r.text
    return r.json()


async def _until_empty(client: AsyncClient, op: str) -> None:
    for _ in range(200):
        if not (await _running(client, op))["tasks"]:
            return
        await asyncio.sleep(0.02)
    pytest.fail("the running list never emptied")


def _done(task: Task) -> Task:
    assert task.status.message is not None
    return Task(
        id=task.id,
        contextId=task.context_id,
        status=TaskStatus(state="completed", message=Message(role="agent", parts=[TextPart(text="done")])),
        history=[*task.history, task.status.message],
        metadata=task.metadata,
    )


class _Gated:
    """Stands in for echo and holds every task until `release`."""

    name = "echo"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def handle_task(self, task: Task) -> Task:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return _done(task)

    async def astream_task(self, task: Task):
        self.started.set()
        yield "one"
        await self.release.wait()
        yield "two"


@pytest.fixture
def gated(app: FastAPI) -> _Gated:
    g = _Gated()
    app.state.dispatch_router.register_local(g)
    return g


# ---- the registry --------------------------------------------------------------


def test_admits_only_an_agent_with_a_running_task_while_draining() -> None:
    from datetime import UTC, datetime

    r = RunningTasks()
    assert r.admits("agent:alice") and r.admits(None)
    r.add(RunningTask("t1", "wright-tertius", "operator:ergon", datetime.now(UTC)))
    r.start_drain(None)
    assert not r.admits("agent:alice")
    assert not r.admits("operator:ergon")  # the dispatcher of the work, not its doer
    assert not r.admits(None)
    assert r.admits("agent:wright-tertius")  # ergon's colleague pass
    assert r.admits("wright-tertius")  # a persona token's bare name
    r.remove("t1")
    r.remove("t1")  # idempotent
    assert not r.admits("agent:wright-tertius")
    assert r.cancel_drain() and r.admits("agent:alice") and not r.cancel_drain()


def test_a_deadline_is_reported_never_acted_on() -> None:
    r = RunningTasks()
    r.start_drain(3600)
    assert r.drain_state()["drain_deadline"] and not r.deadline_passed()
    r.start_drain(0)  # moves the deadline of the same drain
    assert r.deadline_passed() and r.draining


# ---- listing -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_detached_and_a_sync_dispatch_are_listed_while_they_run(
    app: FastAPI, client: AsyncClient, gated: _Gated
) -> None:
    op = await _operator(app, client)
    alice = await _token(app, "agent:alice")
    assert (await _running(client, op)) == {
        "draining": False,
        "drain_started_at": None,
        "drain_deadline": None,
        "deadline_passed": False,
        "tasks": [],
        "orphan_rows": 0,
        "scheduled": 0,
    }
    detached = (await _send(client, alice)).json()["result"]["task"]
    sync = asyncio.create_task(_send(client, alice, detach=False))
    for _ in range(200):
        if len((await _running(client, op))["tasks"]) == 2:
            break
        await asyncio.sleep(0.02)
    tasks = {t["task_id"]: t for t in (await _running(client, op))["tasks"]}
    assert len(tasks) == 2, tasks
    d = tasks.pop(detached["id"])
    assert (d["target_agent"], d["caller_agent"], d["detached"], d["streaming"]) == (
        "echo",
        "agent:alice",
        True,
        False,
    )
    assert d["state"] in ("submitted", "working")
    (s,) = tasks.values()
    assert (s["detached"], s["streaming"]) == (False, False)

    gated.release.set()
    assert (await sync).status_code == 200
    await _until_empty(client, op)


@pytest.mark.asyncio
async def test_a_stream_is_listed_until_it_ends_even_when_dropped(
    app: FastAPI, client: AsyncClient, gated: _Gated
) -> None:
    op = await _operator(app, client)
    router = app.state.dispatch_router
    token = await _token(app, "agent:alice", aud="echo")
    task = Task(
        id="t-stream",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text="hi")])),
    )
    stream = router.dispatch_streaming(
        target_agent="echo", target_generation=None, task=task, caller_jwt=token
    )
    assert await stream.__anext__() == "one"
    (listed,) = (await _running(client, op))["tasks"]
    assert (listed["task_id"], listed["streaming"]) == ("t-stream", True)
    await stream.aclose()  # the client went away mid-stream
    assert (await _running(client, op))["tasks"] == []


@pytest.mark.asyncio
async def test_a_row_left_working_with_no_run_is_an_orphan_not_a_task(
    app: FastAPI, client: AsyncClient
) -> None:
    op = await _operator(app, client)
    async with app.state.sessionmaker() as s:
        echo = await bulletin_store.get_active(s, "echo")
        assert echo is not None
        s.add(
            Conversation(
                task_id="t-orphan",
                target_agent="echo",
                target_generation=echo.generation,
                caller_agent="agent:alice",
                state="working",
            )
        )
        await s.commit()
    body = await _running(client, op)
    assert body["tasks"] == [] and body["orphan_rows"] == 1


@pytest.mark.asyncio
async def test_only_the_operator_sees_or_drains(app: FastAPI, client: AsyncClient) -> None:
    alice = await _token(app, "agent:alice")
    for method, path in (
        ("GET", "/admin/tasks/running"),
        ("POST", "/admin/drain"),
        ("POST", "/admin/drain/cancel"),
    ):
        r = await client.request(method, path, headers={"Authorization": f"Bearer {alice}"})
        assert r.status_code in (401, 403), (path, r.status_code)
        r = await client.request(method, path)
        assert r.status_code in (401, 403), (path, r.status_code)
    assert not app.state.dispatch_router.running.draining


# ---- draining ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draining_refuses_new_work_on_every_route(app: FastAPI, client: AsyncClient) -> None:
    op = await _operator(app, client)
    alice = await _token(app, "agent:alice")
    r = await client.post("/admin/drain", json={}, headers={"Authorization": f"Bearer {op}"})
    assert r.status_code == 200 and r.json()["draining"] is True
    assert (await client.get("/healthz")).json()["draining"] is True

    # A2A v1, detached and not.
    for detach in (True, False):
        body = (await _send(client, alice, detach=detach)).json()
        assert body["error"]["code"] == -32004, body
        assert "draining" in body["error"]["message"]
    assert app.state.detached._reserved == 0  # the refused detach gave its slot back

    # A stream is refused before it yields, and leaves nothing listed.
    task = Task(
        id="t-stream-refused",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text="hi")])),
    )
    stream = app.state.dispatch_router.dispatch_streaming(
        target_agent="echo", target_generation=None, task=task, caller_jwt=await _token(app, "agent:alice", aud="echo")
    )
    with pytest.raises(DrainingError):
        await stream.__anext__()
    assert "t-stream-refused" not in app.state.dispatch_router.running

    # Legacy JSON-RPC message/send.
    envelope = {
        "jsonrpc": "2.0",
        "id": "r1",
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    }
    body = (
        await client.post("/agents/echo", json=envelope, headers={"Authorization": f"Bearer {op}"})
    ).json()
    assert body["error"]["code"] == -32004 and "draining" in body["error"]["message"]

    # POST /a2a/tasks: 503 with Retry-After.
    r = await client.post(
        "/a2a/tasks",
        json={
            "target": "echo",
            "task": {
                "id": "t-legacy",
                "status": {
                    "state": "submitted",
                    "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
                },
            },
        },
        headers={"Authorization": f"Bearer {op}"},
    )
    assert r.status_code == 503 and r.headers["retry-after"] == "60"

    # No refused dispatch left a row behind.
    async with app.state.sessionmaker() as s:
        assert (await s.execute(select(Conversation))).scalars().all() == []

    # Cancel, and work is taken again.
    r = await client.post("/admin/drain/cancel", headers={"Authorization": f"Bearer {op}"})
    assert r.json()["draining"] is False
    assert "result" in (await _send(client, alice, detach=False)).json()


@pytest.mark.asyncio
async def test_a_busy_agent_may_still_dispatch_and_a_parent_claim_counts_for_nothing(
    app: FastAPI, client: AsyncClient, gated: _Gated
) -> None:
    op = await _operator(app, client)
    alice = await _token(app, "agent:alice")
    first = (await _send(client, alice)).json()["result"]["task"]
    await asyncio.wait_for(gated.started.wait(), timeout=5)
    await client.post("/admin/drain", json={"deadline_s": 0}, headers={"Authorization": f"Bearer {op}"})

    # alice dispatched the work but isn't doing it. A parent claim in metadata
    # doesn't change that: inbound strips it (PR 402), and the rule keys on the
    # token subject regardless, which is what this guards.
    forged = (await _send(client, alice, metadata={"cabal_parent_task": first["id"]})).json()
    assert forged["error"]["code"] == -32004, forged

    # echo is running a task, so a dispatch from echo is part of that work.
    echo = await _token(app, "agent:echo")
    sub = (await _send(client, echo)).json()
    assert "result" in sub, sub

    body = await _running(client, op)
    assert body["deadline_passed"] is True and len(body["tasks"]) == 2  # nothing aborted
    gated.release.set()
    await _until_empty(client, op)
    async with app.state.sessionmaker() as s:
        states = {r.task_id: r.state for r in (await s.execute(select(Conversation))).scalars()}
    assert states == {first["id"]: "completed", sub["result"]["task"]["id"]: "completed"}


@pytest.mark.asyncio
async def test_a_restart_flags_its_failed_tasks_as_interrupted(app: FastAPI) -> None:
    import os

    from cabal.core.app import create_app

    async with app.state.sessionmaker() as s:
        echo = await bulletin_store.get_active(s, "echo")
        assert echo is not None
        s.add(
            Conversation(
                task_id="t-cut",
                target_agent="echo",
                target_generation=echo.generation,
                caller_agent="agent:alice",
                state="working",
            )
        )
        await s.commit()
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    restarted = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    async with restarted.router.lifespan_context(restarted):
        pass
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(Conversation).where(Conversation.task_id == "t-cut"))
        ).scalar_one()
    assert row.state == "failed"
    assert row.result_json["metadata"]["cabal_interrupted"] is True


# ---- the CLI -------------------------------------------------------------------

runner = CliRunner()

_BUSY = {
    "draining": True,
    "drain_started_at": "2026-11-10T20:00:00+00:00",
    "drain_deadline": None,
    "deadline_passed": False,
    "tasks": [
        {
            "task_id": "t1",
            "target_agent": "wright-tertius",
            "caller_agent": "operator:ergon",
            "state": "working",
            "started_at": "2026-11-10T19:00:00+00:00",
            "detached": True,
            "streaming": False,
            "parent_task": None,
        }
    ],
    "orphan_rows": 0,
}


@pytest.fixture
def cli(monkeypatch):
    from cabal.cli.main import app as cbl

    monkeypatch.setattr("cabal.cli.main.api.TokenStore.load", lambda self: "tok")
    return cbl


def test_cbl_tasks_lists_running_work(cli, monkeypatch) -> None:
    monkeypatch.setattr("cabal.cli.main.api.running_tasks", lambda **_: _BUSY)
    out = runner.invoke(cli, ["tasks", "--running"])
    assert out.exit_code == 0, out.output
    assert "wright-tertius" in out.output and "detached" in out.output and "draining since" in out.output
    out = runner.invoke(cli, ["tasks", "--json"])
    assert json.loads(out.output) == _BUSY


def test_cbl_drain_wait_exits_3_when_the_deadline_passes(cli, monkeypatch) -> None:
    sent: dict[str, Any] = {}

    def start(**kw: Any) -> dict[str, Any]:
        sent.update(kw)
        return _BUSY

    monkeypatch.setattr("cabal.cli.main.api.start_drain", start)
    monkeypatch.setattr(
        "cabal.cli.main.api.running_tasks", lambda **_: {**_BUSY, "deadline_passed": True}
    )
    out = runner.invoke(cli, ["drain", "--deadline", "60", "--wait", "--poll", "0.1"])
    assert out.exit_code == 3, out.output
    assert sent["deadline_s"] == 60


def test_cbl_drain_wait_exits_0_once_idle(cli, monkeypatch) -> None:
    monkeypatch.setattr("cabal.cli.main.api.start_drain", lambda **_: _BUSY)
    monkeypatch.setattr("cabal.cli.main.api.running_tasks", lambda **_: {**_BUSY, "tasks": []})
    out = runner.invoke(cli, ["drain", "--wait", "--poll", "0.1"])
    assert out.exit_code == 0, out.output
    assert "(no tasks running)" in out.output


def test_cbl_status_shows_the_running_line(cli, monkeypatch) -> None:
    monkeypatch.setattr("cabal.cli.main.api.list_agents", lambda **_: {"agents": []})
    monkeypatch.setattr("cabal.cli.main.api.running_tasks", lambda **_: _BUSY)
    out = runner.invoke(cli, ["status"])
    assert out.exit_code == 0 and "running: 1 task (draining)" in out.output


# ---- the registry's lifetime covers the task's (PR 407 review) -----------------


class _Hold:
    """Wraps one Bookkeeper method so a test can hold it mid-call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, bookkeeper: Any, method: str) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        real = getattr(bookkeeper, method)

        async def held(*args: Any, **kw: Any) -> Any:
            self.reached.set()
            await self.release.wait()
            return await real(*args, **kw)

        monkeypatch.setattr(bookkeeper, method, held)


@pytest.mark.asyncio
async def test_a_task_is_listed_until_its_result_is_written(
    app: FastAPI, client: AsyncClient, gated: _Gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = await _operator(app, client)
    hold = _Hold(monkeypatch, app.state.dispatch_router._bookkeeper, "on_dispatch_success")
    gated.release.set()  # the persona answers at once; the result write is held
    sync = asyncio.create_task(_send(client, await _token(app, "agent:alice"), detach=False))
    await asyncio.wait_for(hold.reached.wait(), timeout=5)
    (listed,) = (await _running(client, op))["tasks"]
    assert listed["state"] == "working"  # the row isn't written yet, so stopping now would lose it
    hold.release.set()
    assert (await sync).status_code == 200
    await _until_empty(client, op)
    async with app.state.sessionmaker() as s:
        assert [r.state for r in (await s.execute(select(Conversation))).scalars()] == ["completed"]


@pytest.mark.asyncio
async def test_a_drain_sees_a_task_admitted_before_its_row_is_written(
    app: FastAPI, client: AsyncClient, gated: _Gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = await _operator(app, client)
    hold = _Hold(monkeypatch, app.state.dispatch_router._bookkeeper, "on_dispatch_start")
    sync = asyncio.create_task(_send(client, await _token(app, "agent:alice"), detach=False))
    await asyncio.wait_for(hold.reached.wait(), timeout=5)
    r = await client.post("/admin/drain", json={}, headers={"Authorization": f"Bearer {op}"})
    assert len(r.json()["tasks"]) == 1
    hold.release.set()
    gated.release.set()
    assert (await sync).status_code == 200
    await _until_empty(client, op)


@pytest.mark.asyncio
async def test_a_start_that_fails_after_admission_leaves_nothing_listed(
    app: FastAPI, gated: _Gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = app.state.dispatch_router

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(router._bookkeeper, "on_dispatch_start", boom)
    task = Task(
        id="t-boom",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text="x")])),
    )
    with pytest.raises(RuntimeError):
        await router.start_detached(
            target_agent="echo", target_generation=None, task=task, caller_jwt=await _token(app, "agent:alice", aud="echo")
        )
    assert "t-boom" not in router.running


@pytest.mark.asyncio
async def test_a_detached_run_cancelled_before_its_first_step_leaves_the_list(
    app: FastAPI, gated: _Gated
) -> None:
    router, detached = app.state.dispatch_router, app.state.detached
    task = Task(
        id="t-early",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text="x")])),
    )
    task, run = await router.start_detached(
        target_agent="echo", target_generation=None, task=task, caller_jwt=await _token(app, "agent:alice", aud="echo")
    )
    assert task.id in router.running
    assert detached.try_reserve()
    detached.spawn(task.id, run)
    detached._running[task.id].cancel()  # before the loop ever runs it, so no finally of its own
    for _ in range(50):
        if task.id not in router.running:
            break
        await asyncio.sleep(0.01)
    assert task.id not in router.running
    assert gated.calls == 0


class _Resumable:
    name = "echo"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_task(self, task: Task, resume_from: Any = None) -> Task:
        self.started.set()
        await self.release.wait()
        return _done(task)


@pytest.mark.asyncio
async def test_a_resume_is_listed_until_its_result_is_written(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = app.state.dispatch_router
    persona = _Resumable()
    router.register_local(persona)
    written: list[str] = []

    async def success(task: Task, response: Task) -> None:
        assert task.id in router.running  # still listed while the result is written
        written.append(response.status.state)

    monkeypatch.setattr(router._bookkeeper, "on_dispatch_success", success)
    task = Task(
        id="t-resume",
        status=TaskStatus(state="working", message=Message(role="user", parts=[TextPart(text="x")])),
    )
    run = asyncio.create_task(router.resume_local(target_agent="echo", task=task, resume_from=None))
    await asyncio.wait_for(persona.started.wait(), timeout=5)
    (listed,) = router.running.list()
    assert (listed.task_id, listed.caller_agent) == ("t-resume", None)
    persona.release.set()
    await run
    assert written == ["completed"] and "t-resume" not in router.running


@pytest.mark.asyncio
async def test_a_retry_with_a_running_tasks_id_is_refused_and_unlists_nothing(
    app: FastAPI, client: AsyncClient, gated: _Gated
) -> None:
    op = await _operator(app, client)
    first = (await _send(client, await _token(app, "agent:alice"))).json()["result"]["task"]
    await asyncio.wait_for(gated.started.wait(), timeout=5)
    r = await client.post(
        "/a2a/tasks",
        json={
            "target": "echo",
            "task": {
                "id": first["id"],
                "status": {
                    "state": "submitted",
                    "message": {"role": "user", "parts": [{"kind": "text", "text": "again"}]},
                },
            },
        },
        headers={"Authorization": f"Bearer {op}"},
    )
    assert r.status_code == 409 and "already running" in r.text
    assert [t["task_id"] for t in (await _running(client, op))["tasks"]] == [first["id"]]
    assert gated.calls == 1
    gated.release.set()
    await _until_empty(client, op)


@pytest.mark.asyncio
async def test_a_stream_is_listed_before_its_row_is_written(
    app: FastAPI, gated: _Gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = app.state.dispatch_router
    hold = _Hold(monkeypatch, router._bookkeeper, "on_dispatch_start")
    task = Task(
        id="t-stream-early",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text="hi")])),
    )
    stream = router.dispatch_streaming(
        target_agent="echo", target_generation=None, task=task, caller_jwt=await _token(app, "agent:alice", aud="echo")
    )
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.wait_for(hold.reached.wait(), timeout=5)
    assert "t-stream-early" in router.running
    hold.release.set()
    assert await first == "one"
    await stream.aclose()
    assert "t-stream-early" not in router.running


@pytest.mark.asyncio
async def test_a_stream_or_resume_reusing_a_running_tasks_id_unlists_nothing(
    app: FastAPI, client: AsyncClient, gated: _Gated
) -> None:
    router = app.state.dispatch_router
    first = (await _send(client, await _token(app, "agent:alice"))).json()["result"]["task"]
    await asyncio.wait_for(gated.started.wait(), timeout=5)
    again = Task(
        id=first["id"],
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text="again")])),
    )
    stream = router.dispatch_streaming(
        target_agent="echo", target_generation=None, task=again, caller_jwt=await _token(app, "agent:alice", aud="echo")
    )
    with pytest.raises(TaskAlreadyRunning):
        await stream.__anext__()
    assert first["id"] in router.running
    with pytest.raises(TaskAlreadyRunning):
        await router.resume_local(target_agent="echo", task=again, resume_from=None)
    (listed,) = router.running.list()
    assert (listed.task_id, listed.detached, gated.calls) == (first["id"], True, 1)
    gated.release.set()
    for _ in range(200):
        if first["id"] not in router.running:
            break
        await asyncio.sleep(0.02)
    assert first["id"] not in router.running


def test_remove_with_an_entry_removes_only_that_entry() -> None:
    reg = RunningTasks()
    mine = RunningTask(task_id="t", target_agent="a", caller_agent=None, started_at=datetime.now(UTC))
    other = RunningTask(task_id="t", target_agent="a", caller_agent=None, started_at=datetime.now(UTC))
    reg.add(mine)
    reg.remove("t", other)
    assert reg.get("t") is mine
    reg.remove("t", mine)
    assert "t" not in reg


@pytest.mark.asyncio
async def test_a_detached_runs_done_callback_leaves_a_same_id_run_listed(
    app: FastAPI, client: AsyncClient, gated: _Gated
) -> None:
    """Between a detached run's own finally and its done-callback, the id is
    free; a run listed there under the same id must stay listed (PR 407 r4)."""
    router = app.state.dispatch_router
    first = (await _send(client, await _token(app, "agent:alice"))).json()["result"]["task"]
    await asyncio.wait_for(gated.started.wait(), timeout=5)
    fresh = RunningTask(task_id=first["id"], target_agent="echo", caller_agent=None, started_at=datetime.now(UTC))

    async def racer() -> None:
        while first["id"] in router.running:  # noqa: ASYNC110 -- polls a sync registry each loop turn, as the race needs
            await asyncio.sleep(0)
        router.running.add(fresh)  # what _track or resume_local does once the id is free

    race = asyncio.create_task(racer())
    gated.release.set()
    await asyncio.wait_for(race, timeout=5)
    for _ in range(20):  # let the done-callback run
        await asyncio.sleep(0.01)
    assert router.running.get(first["id"]) is fresh
