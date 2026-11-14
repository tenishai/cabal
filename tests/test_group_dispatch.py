"""Dispatch to a group: the task waits for the first idle member (cabal issue 423)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from typer.testing import CliRunner

from cabal.config.identity import IdentityConfig
from cabal.core.a2a.types import Task
from cabal.core.models import Conversation
from cabal.core.running import RunningTask
from tests.test_schedule import _operator, _row, _token, _until_state

GROUP = "pair"


@pytest_asyncio.fixture
async def gapp(monkeypatch) -> FastAPI:
    """The test app with two local personas, echo and tck, as group `pair`."""
    from cabal.agents.tck import TCK_ENV
    from cabal.core.app import create_app

    monkeypatch.setenv(TCK_ENV, "1")
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    app = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    async with app.router.lifespan_context(app):
        app.state.identity = IdentityConfig(groups={GROUP: ["echo", "tck"], "solo": ["tck"]})
        yield app


@pytest_asyncio.fixture
async def gclient(gapp: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=gapp), base_url="http://test") as c:
        yield c


async def _send(
    client: AsyncClient, token: str, *, via: str = "echo", group: Any = GROUP, detach: bool = True,
    extra: dict[str, Any] | None = None,
) -> Response:
    params: dict[str, Any] = {
        "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "work"}]},
        "metadata": {"cabal_group": group, **(extra or {})},
    }
    if detach:
        params["configuration"] = {"returnImmediately": True}
    return await client.post(
        f"/agents/{via}/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": params},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )


async def _queue(app: FastAPI, client: AsyncClient, token: str | None = None) -> str:
    body = (await _send(client, token or await _token(app))).json()
    assert "result" in body, body
    return str(body["result"]["task"]["id"])


def _busy(app: FastAPI, agent: str) -> None:
    app.state.dispatch_router.running.add(
        RunningTask(task_id=f"busy-{agent}", target_agent=agent, caller_agent=None, started_at=datetime.now(UTC))
    )


def _idle(app: FastAPI, agent: str) -> None:
    app.state.dispatch_router.running.remove(f"busy-{agent}")



def _later() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=1)


# ---- asking -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_group_task_is_recorded_waiting_for_a_member(gapp: FastAPI, gclient: AsyncClient) -> None:
    task_id = await _queue(gapp, gclient)
    row = await _row(gapp, task_id)
    assert (row.state, row.target_group, row.target_agent) == ("scheduled", GROUP, "echo")
    assert row.not_before is not None
    assert gapp.state.dispatch_router.running.list() == []
    meta = row.result_json["metadata"]
    assert meta["cabal_group"] == GROUP and "cabal_not_before" not in meta


@pytest.mark.asyncio
async def test_bad_group_requests_are_refused(gapp: FastAPI, gclient: AsyncClient) -> None:
    token = await _token(gapp)
    body = (await _send(gclient, token, group="nobody")).json()
    assert body["error"]["code"] == -32602 and "not a group" in body["error"]["message"]
    body = (await _send(gclient, token, group=["pair"])).json()
    assert body["error"]["code"] == -32602
    body = (await _send(gclient, token, group="solo", via="echo")).json()
    assert "echo is not a member of group 'solo'" in body["error"]["message"]
    body = (await _send(gclient, token, detach=False)).json()
    assert body["error"]["code"] == -32004 and "returnImmediately" in body["error"]["message"]
    async with gapp.state.sessionmaker() as s:
        assert (await s.execute(select(Conversation))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_group_task_can_also_wait_for_a_time(gapp: FastAPI, gclient: AsyncClient) -> None:
    when = datetime.now(UTC) + timedelta(hours=1)
    body = (await _send(gclient, await _token(gapp), extra={"cabal_not_before": when.isoformat()})).json()
    task_id = body["result"]["task"]["id"]
    assert await gapp.state.scheduler.tick() == 0
    row = await _row(gapp, task_id)
    assert row.result_json["metadata"]["cabal_not_before"] == when.isoformat()
    assert await gapp.state.scheduler.tick(now=when) == 1


# ---- starting -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_starts_on_the_first_idle_member(gapp: FastAPI, gclient: AsyncClient) -> None:
    task_id = await _queue(gapp, gclient)
    _busy(gapp, "echo")
    assert await gapp.state.scheduler.tick(now=_later()) == 1
    row = await _until_state(gapp, task_id, "completed")
    assert row.target_agent == "tck"
    assert row.target_generation is not None
    # tck ran it, not echo, the member it was sent through.
    assert "Hello from TCK" in str(row.result_json) and "echo: work" not in str(row.result_json)


@pytest.mark.asyncio
async def test_with_both_idle_the_first_listed_takes_it(gapp: FastAPI, gclient: AsyncClient) -> None:
    task_id = await _queue(gapp, gclient)
    assert await gapp.state.scheduler.tick(now=_later()) == 1
    assert (await _until_state(gapp, task_id, "completed")).target_agent == "echo"


@pytest.mark.asyncio
async def test_while_every_member_is_busy_it_waits_and_others_start(
    gapp: FastAPI, gclient: AsyncClient
) -> None:
    token = await _token(gapp)
    task_id = await _queue(gapp, gclient, token)
    # An ordinary scheduled task, due after the group task.
    plain = (
        await gclient.post(
            "/agents/echo/a2a",
            json={"jsonrpc": "2.0", "id": 8, "method": "SendMessage", "params": {
                "message": {"messageId": "m-2", "role": "ROLE_USER", "parts": [{"text": "x"}]},
                "metadata": {"cabal_not_before": (datetime.now(UTC) + timedelta(milliseconds=200)).isoformat()},
                "configuration": {"returnImmediately": True}}},
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
        )
    ).json()["result"]["task"]["id"]
    _busy(gapp, "echo")
    _busy(gapp, "tck")
    later = datetime.now(UTC) + timedelta(seconds=2)
    assert await gapp.state.scheduler.tick(now=later) == 1  # the plain one; the group one can't
    await _until_state(gapp, plain, "completed")
    assert (await _row(gapp, task_id)).state == "scheduled"
    _idle(gapp, "tck")
    assert await gapp.state.scheduler.tick(now=later) == 1
    assert (await _until_state(gapp, task_id, "completed")).target_agent == "tck"


@pytest.mark.asyncio
async def test_two_group_tasks_in_one_tick_go_to_different_members(
    gapp: FastAPI, gclient: AsyncClient, monkeypatch
) -> None:
    release = asyncio.Event()
    router = gapp.state.dispatch_router

    def held(handler: Any) -> Any:
        real = handler.handle_task

        async def wait_then(task: Task) -> Task:
            await release.wait()
            return await real(task)

        return wait_then

    for name in ("echo", "tck"):
        monkeypatch.setattr(router._local[name], "handle_task", held(router._local[name]))
    token = await _token(gapp)
    first, second = await _queue(gapp, gclient, token), await _queue(gapp, gclient, token)
    third = await _queue(gapp, gclient, token)
    assert await gapp.state.scheduler.tick(now=_later()) == 2
    targets = {(await _row(gapp, t)).target_agent for t in (first, second)}
    assert targets == {"echo", "tck"}
    assert (await _row(gapp, third)).state == "scheduled"  # both members are busy now
    release.set()
    for t in (first, second):
        await _until_state(gapp, t, "completed")


@pytest.mark.asyncio
async def test_a_group_dropped_from_config_fails_its_task(gapp: FastAPI, gclient: AsyncClient) -> None:
    task_id = await _queue(gapp, gclient)
    gapp.state.identity = IdentityConfig(groups={})
    assert await gapp.state.scheduler.tick(now=_later()) == 0
    row = await _until_state(gapp, task_id, "failed")
    assert "no group 'pair' in [groups]" in str(row.result_json)


@pytest.mark.asyncio
async def test_a_member_with_no_active_agent_is_skipped(gapp: FastAPI, gclient: AsyncClient) -> None:
    gapp.state.identity = IdentityConfig(groups={GROUP: ["ghost", "echo", "tck"]})
    task_id = await _queue(gapp, gclient)
    assert await gapp.state.scheduler.tick(now=_later()) == 1
    assert (await _until_state(gapp, task_id, "completed")).target_agent == "echo"


# ---- reading and cancelling ----------------------------------------------------------


async def _get(client: AsyncClient, token: str, via: str, task_id: str) -> dict[str, Any]:
    return (
        await client.post(
            f"/agents/{via}/a2a",
            json={"jsonrpc": "2.0", "id": 9, "method": "GetTask", "params": {"id": task_id}},
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
        )
    ).json()


@pytest.mark.asyncio
async def test_every_member_can_read_it_and_others_cannot(gapp: FastAPI, gclient: AsyncClient) -> None:
    token = await _token(gapp)
    task_id = await _queue(gapp, gclient, token)
    for via in ("echo", "tck"):
        assert _get_state(await _get(gclient, token, via, task_id)) == "TASK_STATE_SUBMITTED"
    _busy(gapp, "echo")
    await gapp.state.scheduler.tick(now=_later())
    await _until_state(gapp, task_id, "completed")
    # Started on tck; the echo route still finds it, since it is a group task.
    assert _get_state(await _get(gclient, token, "echo", task_id)) == "TASK_STATE_COMPLETED"
    gapp.state.identity = IdentityConfig(groups={GROUP: ["tck"]})
    assert "error" in await _get(gclient, token, "echo", task_id)


def _get_state(body: dict[str, Any]) -> str:
    assert "result" in body, body
    return str(body["result"]["status"]["state"])


@pytest.mark.asyncio
async def test_a_waiting_group_task_can_be_cancelled(gapp: FastAPI, gclient: AsyncClient) -> None:
    token = await _token(gapp)
    task_id = await _queue(gapp, gclient, token)
    body = (
        await gclient.post(
            "/agents/tck/a2a",
            json={"jsonrpc": "2.0", "id": 10, "method": "CancelTask", "params": {"id": task_id}},
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
        )
    ).json()
    assert body["result"]["status"]["state"] == "TASK_STATE_CANCELED", body
    assert await gapp.state.scheduler.tick(now=_later()) == 0


@pytest.mark.asyncio
async def test_listing_shows_the_group_and_groups_are_listed(gapp: FastAPI, gclient: AsyncClient) -> None:
    op = await _operator(gapp, gclient)
    task_id = await _queue(gapp, gclient)
    auth = {"Authorization": f"Bearer {op}"}
    (t,) = (await gclient.get("/admin/tasks/scheduled", headers=auth)).json()["tasks"]
    assert (t["task_id"], t["target_group"]) == (task_id, GROUP)
    assert (await gclient.get("/admin/groups", headers=auth)).json() == {
        "groups": {GROUP: ["echo", "tck"], "solo": ["tck"]}
    }
    r = await gclient.get("/admin/groups", headers={"Authorization": f"Bearer {await _token(gapp)}"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_without_identity_there_are_no_groups(app: FastAPI, client: AsyncClient) -> None:
    op = await _operator(app, client)
    assert (await client.get("/admin/groups", headers={"Authorization": f"Bearer {op}"})).json() == {"groups": {}}


# ---- the CLI ------------------------------------------------------------------------

runner = CliRunner()


def test_cbl_dispatch_to_a_group(monkeypatch) -> None:
    from cabal.cli.main import app as cbl

    monkeypatch.setattr("cabal.cli.main.api.TokenStore.load", lambda self: "tok")
    out = runner.invoke(cbl, ["dispatch", "group:pair", "hi"])
    assert out.exit_code == 2 and "--detach" in out.output
    monkeypatch.setattr("cabal.cli.main.api.groups", lambda **_: {"pair": ["echo", "tck"]})
    sent: dict[str, Any] = {}

    def fake(**kw: Any) -> dict[str, Any]:
        sent.update(kw)
        return {"id": "t-1", "status": {"state": "TASK_STATE_SUBMITTED"}, "metadata": {}}

    monkeypatch.setattr("cabal.cli.main.api.dispatch_detached", fake)
    out = runner.invoke(cbl, ["dispatch", "--detach", "--metadata", '{"k": 1}', "group:pair", "hi"])
    assert out.exit_code == 0, out.output
    assert sent["target"] == "echo" and sent["metadata"] == {"k": 1, "cabal_group": "pair"}
    assert "for the next idle member of pair" in out.output
    out = runner.invoke(cbl, ["dispatch", "--detach", "group:nobody", "hi"])
    assert out.exit_code == 1 and "no group 'nobody'" in out.output


def test_cbl_tasks_scheduled_shows_the_group(monkeypatch) -> None:
    from cabal.cli.main import app as cbl

    monkeypatch.setattr("cabal.cli.main.api.TokenStore.load", lambda self: "tok")
    monkeypatch.setattr(
        "cabal.cli.main.api.scheduled_tasks",
        lambda **_: {"tasks": [{"task_id": "t-1", "target_agent": "echo", "target_group": "pair",
                                "caller_agent": "operator", "not_before": "2026-11-14T01:00:00+00:00"}]},
    )
    out = runner.invoke(cbl, ["tasks", "--scheduled"])
    assert out.exit_code == 0 and "group:pair" in out.output


def test_migration_adds_and_drops_target_group(tmp_path, monkeypatch) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from cabal.core.app import _migrations_dir

    db = tmp_path / "m.db"
    monkeypatch.setenv("CABAL_DB_URL", f"sqlite:///{db}")
    cfg = Config()
    cfg.set_main_option("script_location", str(_migrations_dir()))

    def columns() -> set[str]:
        return {c["name"] for c in inspect(create_engine(f"sqlite:///{db}")).get_columns("conversations")}

    command.upgrade(cfg, "head")
    assert "target_group" in columns()
    command.downgrade(cfg, "a9d3e5f7c1b2")
    assert "target_group" not in columns()
    command.upgrade(cfg, "head")
    assert "target_group" in columns()
