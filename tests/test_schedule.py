"""Dispatch with a not-before time (cabal issue 406)."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import typer
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select
from typer.testing import CliRunner

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode, Conversation

SOON = timedelta(hours=1)


async def _operator(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _token(app: FastAPI, sub: str = "agent:alice", **kw: Any) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    kw.setdefault("ttl_seconds", 86400)  # mint's default is 5 minutes, shorter than SOON
    aud = kw.pop("aud", "cabal-core")
    scope = ["colleague"] if aud == "cabal-core" else ["dispatch"]
    return mint(signing_key=stored, sub=sub, aud=aud, scope=scope, **kw)


async def _send(
    client: AsyncClient, token: str, *, when: Any, detach: bool = True, method: str = "SendMessage"
) -> Response:
    params: dict[str, Any] = {
        "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "later"}]},
        "metadata": {"cabal_not_before": when},
    }
    if detach:
        params["configuration"] = {"returnImmediately": True}
    return await client.post(
        "/agents/echo/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )


async def _row(app: FastAPI, task_id: str) -> Conversation:
    async with app.state.sessionmaker() as s:
        return (await s.execute(select(Conversation).where(Conversation.task_id == task_id))).scalar_one()


async def _until_state(app: FastAPI, task_id: str, state: str) -> Conversation:
    for _ in range(200):
        row = await _row(app, task_id)
        if row.state == state:
            return row
        await asyncio.sleep(0.02)
    pytest.fail(f"{task_id} never reached {state}; it is {row.state}")


async def _schedule(app: FastAPI, client: AsyncClient, token: str | None = None) -> tuple[str, datetime]:
    token = token or await _token(app)
    when = datetime.now(UTC) + SOON
    body = (await _send(client, token, when=when.isoformat())).json()
    assert "result" in body, body
    return body["result"]["task"]["id"], when


# ---- asking -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_future_time_is_recorded_scheduled_with_no_token(app: FastAPI, client: AsyncClient) -> None:
    token = await _token(app)
    when = datetime.now(UTC) + SOON
    body = (await _send(client, token, when=when.isoformat())).json()
    assert "result" in body, body
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert datetime.fromisoformat(task["metadata"]["cabalNotBefore"]) == when
    row = await _row(app, task["id"])
    assert row.state == "scheduled" and row.caller_agent == "agent:alice"
    assert set(row.schedule_json) <= {"sub", "cabal_persona", "cabal_scope", "aud", "iat", "exp"}
    assert token not in str(row.schedule_json) and token not in str(row.result_json)
    assert app.state.dispatch_router.running.list() == []  # nothing runs yet
    got = (
        await client.post(
            "/agents/echo/a2a",
            json={"jsonrpc": "2.0", "id": 8, "method": "GetTask", "params": {"id": task["id"]}},
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
        )
    ).json()["result"]
    assert got["status"]["state"] == "TASK_STATE_SUBMITTED"


@pytest.mark.asyncio
async def test_bad_requests_are_refused(app: FastAPI, client: AsyncClient) -> None:
    token = await _token(app)
    later = (datetime.now(UTC) + SOON).isoformat()
    body = (await _send(client, token, when=later, detach=False)).json()
    assert body["error"]["code"] == -32004 and "returnImmediately" in body["error"]["message"]
    for bad in ("tomorrow", "2026-11-10T17:00:00", 1700000000):
        body = (await _send(client, token, when=bad)).json()
        assert body["error"]["code"] == -32602, (bad, body)
    short = await _token(app, ttl_seconds=60)
    body = (await _send(client, short, when=later)).json()
    assert body["error"]["code"] == -32602 and "expires" in body["error"]["message"]
    async with app.state.sessionmaker() as s:
        assert (await s.execute(select(Conversation))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_past_time_starts_now(app: FastAPI, client: AsyncClient) -> None:
    token = await _token(app)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    task = (await _send(client, token, when=past)).json()["result"]["task"]
    row = await _until_state(app, task["id"], "completed")
    assert row.schedule_json is None
    assert "cabal_not_before" not in row.result_json["metadata"]


@pytest.mark.asyncio
async def test_the_other_doors_refuse_rather_than_ignore_it(app: FastAPI, client: AsyncClient) -> None:
    op = await _operator(app, client)
    r = await client.post(
        "/a2a/tasks",
        json={
            "target": "echo",
            "task": {
                "id": "t-legacy",
                "status": {"state": "submitted", "message": {"role": "user", "parts": [{"kind": "text", "text": "x"}]}},
                "metadata": {"cabal_not_before": (datetime.now(UTC) + SOON).isoformat()},
            },
        },
        headers={"Authorization": f"Bearer {op}"},
    )
    assert r.status_code == 502 and "only honoured on A2A v1" in r.text


# ---- starting -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_due_task_starts_once_and_records_its_lateness(app: FastAPI, client: AsyncClient) -> None:
    """Two ticks don't truly interleave on one loop here; the claim's
    exactly-once is proven by the cancel race below. This pins lateness and
    that a started row is never started again."""
    task_id, when = await _schedule(app, client)
    sched = app.state.scheduler
    assert await sched.tick() == 0  # not due yet
    assert (await _row(app, task_id)).state == "scheduled"
    late = when + timedelta(seconds=30)
    first, second = await asyncio.gather(sched.tick(now=late), sched.tick(now=late))
    assert first + second == 1
    row = await _until_state(app, task_id, "completed")
    assert row.result_json["metadata"]["cabal_started_late_s"] == pytest.approx(30, abs=1)
    assert await sched.tick(now=late) == 0


@pytest.mark.asyncio
async def test_draining_holds_a_due_task_until_it_ends(app: FastAPI, client: AsyncClient) -> None:
    task_id, when = await _schedule(app, client)
    running = app.state.dispatch_router.running
    running.start_drain(None)
    assert await app.state.scheduler.tick(now=when) == 0
    assert (await _row(app, task_id)).state == "scheduled"
    running.cancel_drain()
    assert await app.state.scheduler.tick(now=when) == 1
    await _until_state(app, task_id, "completed")


@pytest.mark.asyncio
async def test_at_the_detached_cap_it_waits(app: FastAPI, client: AsyncClient, monkeypatch) -> None:
    from cabal.core.detached import DetachedTasks

    task_id, when = await _schedule(app, client)
    full = DetachedTasks(limit=0, abort=app.state.dispatch_router.abort_coordinator)
    monkeypatch.setattr(app.state, "detached", full)
    assert await app.state.scheduler.tick(now=when) == 0
    assert (await _row(app, task_id)).state == "scheduled"


@pytest.mark.asyncio
async def test_a_caller_revoked_after_scheduling_is_failed_with_the_reason(
    app: FastAPI, client: AsyncClient
) -> None:
    op = await _operator(app, client)
    now = int(time.time())
    token = await _token(app, now=now - 20)
    task_id, when = await _schedule(app, client, token)
    # The cutoff falls BETWEEN the two iats: the stored claims (iat now-20) are
    # revoked, but a token minted at start (iat now) would pass the router's own
    # check. Only the stored-claims check can refuse this (PR 408 review).
    r = await client.post(
        "/admin/token/revoke",
        json={"sub": "agent:alice", "not_before": now - 5},
        headers={"Authorization": f"Bearer {op}"},
    )
    assert r.status_code == 200, r.text
    assert await app.state.scheduler.tick(now=when) == 0
    row = await _row(app, task_id)
    assert row.state == "failed"
    text = row.result_json["status"]["message"]["parts"][0]["text"]
    assert "could not start" in text and "revoked" in text.lower()


@pytest.mark.asyncio
async def test_claims_that_expired_before_the_start_fail_the_task(app: FastAPI, client: AsyncClient) -> None:
    """The start-time exp check bounds a scheduled start to the caller's own
    authority, for example when cabal was down past it (PR 408 review)."""
    from sqlalchemy import update

    task_id, when = await _schedule(app, client)
    row = await _row(app, task_id)
    claims = {**row.schedule_json, "exp": int(time.time()) - 60}
    async with open_session(app.state.sessionmaker) as session:
        await session.execute(
            update(Conversation).where(Conversation.task_id == task_id).values(schedule_json=claims)
        )
    assert await app.state.scheduler.tick(now=when) == 0
    row = await _row(app, task_id)
    assert row.state == "failed"
    text = row.result_json["status"]["message"]["parts"][0]["text"]
    assert "expired before the task started" in text


@pytest.mark.asyncio
async def test_a_stream_with_a_not_before_is_refused_not_started_at_once(app: FastAPI) -> None:
    from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
    from cabal.core.broker.router import DispatchError

    seen: list[str] = []

    class Streamer:
        name = "echo"

        async def handle_task(self, task: Task) -> Task:
            return task

        async def astream_task(self, task: Task):
            seen.append(task.id)
            yield "started"

    app.state.dispatch_router.register_local(Streamer())
    task = Task(
        id="t-stream-later",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text="x")])),
        metadata={"cabal_not_before": (datetime.now(UTC) + SOON).isoformat()},
    )
    stream = app.state.dispatch_router.dispatch_streaming(
        target_agent="echo", target_generation=None, task=task, caller_jwt=await _token(app, aud="echo")
    )
    with pytest.raises(DispatchError, match="only honoured"):
        await stream.__anext__()
    assert seen == []


@pytest.mark.asyncio
async def test_a_scheduled_task_can_be_cancelled_and_then_never_starts(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _token(app)
    task_id, when = await _schedule(app, client, token)
    body = (
        await client.post(
            "/agents/echo/a2a",
            json={"jsonrpc": "2.0", "id": 9, "method": "CancelTask", "params": {"id": task_id}},
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
        )
    ).json()
    assert body["result"]["status"]["state"] == "TASK_STATE_CANCELED", body
    assert await app.state.scheduler.tick(now=when) == 0
    assert (await _row(app, task_id)).state == "canceled"


@pytest.mark.asyncio
async def test_a_cancel_racing_the_start_has_one_outcome(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core.scheduler import cancel_scheduled

    task_id, when = await _schedule(app, client)
    started, canceled = await asyncio.gather(
        app.state.scheduler.tick(now=when), cancel_scheduled(app.state.sessionmaker, task_id)
    )
    assert started + int(canceled) == 1
    final = "canceled" if canceled else "completed"
    await _until_state(app, task_id, final)


@pytest.mark.asyncio
async def test_listing_and_the_running_count(app: FastAPI, client: AsyncClient) -> None:
    op = await _operator(app, client)
    task_id, when = await _schedule(app, client)
    listed = (await client.get("/admin/tasks/scheduled", headers={"Authorization": f"Bearer {op}"})).json()
    (t,) = listed["tasks"]
    assert (t["task_id"], t["target_agent"]) == (task_id, "echo")
    assert datetime.fromisoformat(t["not_before"]) == when
    running = (await client.get("/admin/tasks/running", headers={"Authorization": f"Bearer {op}"})).json()
    assert running["scheduled"] == 1 and running["tasks"] == []
    alice = await _token(app)
    r = await client.get("/admin/tasks/scheduled", headers={"Authorization": f"Bearer {alice}"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_a_scheduled_task_survives_a_restart(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core.app import create_app

    task_id, when = await _schedule(app, client)
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    restarted = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    async with restarted.router.lifespan_context(restarted):
        assert (await _row(restarted, task_id)).state == "scheduled"  # the sweep left it
        assert await restarted.state.scheduler.tick(now=when) == 1
        await _until_state(restarted, task_id, "completed")


def test_migration_downgrades_and_upgrades_again(tmp_path, monkeypatch) -> None:
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
    assert {"not_before", "schedule_json"} <= columns()
    command.downgrade(cfg, "-1")
    assert not ({"not_before", "schedule_json"} & columns())
    command.upgrade(cfg, "head")
    assert {"not_before", "schedule_json"} <= columns()


# ---- the CLI ------------------------------------------------------------------------

runner = CliRunner()


def test_parse_when() -> None:
    from cabal.cli.main import parse_when

    now = datetime(2026, 11, 10, 18, 30, tzinfo=UTC).astimezone()
    today = parse_when(f"{(now + timedelta(hours=1)).hour:02d}:00", now=now)
    assert datetime.fromisoformat(today) > now
    passed = parse_when(f"{now.hour:02d}:{now.minute:02d}", now=now)
    assert datetime.fromisoformat(passed) - now == timedelta(days=1)
    assert parse_when("2026-11-10T17:00:00-05:00") == "2026-11-10T17:00:00-05:00"
    assert datetime.fromisoformat(parse_when("2026-11-10T17:00:00")).tzinfo is not None
    for bad in ("25:00", "noon", "17:60"):
        with pytest.raises(typer.BadParameter):
            parse_when(bad, now=now)


def test_dispatch_not_before_needs_detach_and_sends_the_time(monkeypatch) -> None:
    from cabal.cli.main import app as cbl

    monkeypatch.setattr("cabal.cli.main.api.TokenStore.load", lambda self: "tok")
    out = runner.invoke(cbl, ["dispatch", "--not-before", "17:00", "echo", "hi"])
    assert out.exit_code == 2
    sent: dict[str, Any] = {}

    def fake(**kw: Any) -> dict[str, Any]:
        sent.update(kw)
        return {"id": "t-1", "status": {"state": "TASK_STATE_SUBMITTED"},
                "metadata": {"cabalNotBefore": kw["metadata"]["cabal_not_before"]}}

    monkeypatch.setattr("cabal.cli.main.api.dispatch_detached", fake)
    out = runner.invoke(
        cbl, ["dispatch", "--detach", "--not-before", "2026-11-10T17:00:00-05:00",
              "--metadata", '{"k": 1}', "echo", "hi"]
    )
    assert out.exit_code == 0, out.output
    assert sent["metadata"] == {"k": 1, "cabal_not_before": "2026-11-10T17:00:00-05:00"}
    assert "starts at 2026-11-10T17:00:00-05:00" in out.output


def test_cbl_tasks_scheduled_and_task_cancel(monkeypatch) -> None:
    from cabal.cli.main import app as cbl

    monkeypatch.setattr("cabal.cli.main.api.TokenStore.load", lambda self: "tok")
    monkeypatch.setattr(
        "cabal.cli.main.api.scheduled_tasks",
        lambda **_: {"tasks": [{"task_id": "t-1", "target_agent": "wright-tertius",
                                "caller_agent": "operator:ergon", "not_before": "2026-11-10T22:00:00+00:00"}]},
    )
    out = runner.invoke(cbl, ["tasks", "--scheduled"])
    assert out.exit_code == 0 and "wright-tertius" in out.output and "2026-11-10T22:00" in out.output
    monkeypatch.setattr(
        "cabal.cli.main.api.cancel_task", lambda **_: {"status": {"state": "TASK_STATE_CANCELED"}}
    )
    out = runner.invoke(cbl, ["task", "t-1", "--target", "echo", "--cancel"])
    assert out.exit_code == 0 and "[canceled]" in out.output
