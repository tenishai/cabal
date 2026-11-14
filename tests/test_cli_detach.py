"""`cbl dispatch --detach` and `cbl task` (issue #391), on top of the server's
returnImmediately dispatch (#277)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from typer.testing import CliRunner

import cabal.cli.client as api
import cabal.cli.main as cli_main
from cabal.cli.main import app as cli_app
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import create_app
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Persona
from tests.test_cli_login_dispatch import serve

runner = CliRunner()


def _task(state: str, text: str | None = None) -> dict:
    status: dict = {"state": f"TASK_STATE_{state}"}
    if text is not None:
        status["message"] = {"role": "ROLE_AGENT", "parts": [{"text": text}]}
    return {"id": "t-1", "contextId": "c-1", "status": status}


@pytest.fixture
def token(monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text("tok")
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", path)
    monkeypatch.setattr(api, "task_target", lambda **kw: "echo")


class _Script(list):
    sleeps: list[float]

    def __init__(self) -> None:
        super().__init__()
        self.sleeps = []


@pytest.fixture
def polls(monkeypatch):
    """Feed `cbl task` a script of GetTask answers (a RequestError is raised);
    returns the list of sleeps it asked for instead of sleeping."""
    script = _Script()
    sleeps = script.sleeps

    def get_task(**kw):
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(api, "get_task", get_task)
    monkeypatch.setattr(cli_main.time, "sleep", sleeps.append)
    return script


def test_detach_prints_the_id_and_state(token, monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(api, "dispatch_detached", lambda **kw: calls.append(kw) or _task("SUBMITTED"))
    result = runner.invoke(cli_app, ["dispatch", "--detach", "echo", "hi", "--context", "b-1"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "t-1 [submitted]"
    assert calls == [{"token": "tok", "target": "echo", "message_text": "hi", "context_id": "b-1"}]


def test_detach_raw_prints_the_task_json(token, monkeypatch) -> None:
    monkeypatch.setattr(api, "dispatch_detached", lambda **kw: _task("SUBMITTED"))
    result = runner.invoke(cli_app, ["dispatch", "--detach", "--raw", "echo", "hi"])
    assert json.loads(result.output)["id"] == "t-1"


def test_detach_surfaces_a_jsonrpc_error(token, monkeypatch) -> None:
    def refuse(**kw):
        raise api.A2AError(-32004, "too many tasks are running detached")

    monkeypatch.setattr(api, "dispatch_detached", refuse)
    result = runner.invoke(cli_app, ["dispatch", "--detach", "echo", "hi"])
    assert result.exit_code == 1
    assert "too many tasks" in result.output


def test_task_prints_state_and_message(token, polls) -> None:
    polls.append(_task("COMPLETED", "the answer"))
    result = runner.invoke(cli_app, ["task", "t-1"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "[completed] the answer"


def test_task_without_wait_reports_a_running_task_and_exits_zero(token, polls) -> None:
    polls.append(_task("WORKING"))
    result = runner.invoke(cli_app, ["task", "t-1"])
    assert result.exit_code == 0
    assert result.output.strip() == "[working]"


def test_task_raw(token, polls) -> None:
    polls.append(_task("COMPLETED", "x"))
    result = runner.invoke(cli_app, ["task", "t-1", "--raw"])
    assert json.loads(result.output)["status"]["state"] == "TASK_STATE_COMPLETED"


def test_wait_polls_until_completed(token, polls) -> None:
    polls.extend([_task("SUBMITTED"), _task("WORKING"), _task("COMPLETED", "done")])
    result = runner.invoke(cli_app, ["task", "t-1", "--wait", "--interval", "0.5"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "[completed] done"
    assert polls.sleeps == [0.5, 0.5]


@pytest.mark.parametrize(
    "state", ["FAILED", "CANCELED", "REJECTED", "INPUT_REQUIRED", "AUTH_REQUIRED"]
)
def test_wait_exits_nonzero_unless_completed(token, polls, state) -> None:
    polls.extend([_task("WORKING"), _task(state, "why")])
    result = runner.invoke(cli_app, ["task", "t-1", "--wait"])
    assert result.exit_code == 1
    assert "why" in result.output


def test_wait_timeout_is_a_distinct_clear_exit(token, polls, monkeypatch) -> None:
    polls.extend([_task("WORKING")] * 20)
    # Run the clock on the fake sleeps: each sleep advances it.
    clock = [0.0]
    monkeypatch.setattr(cli_main.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(cli_main.time, "monotonic", lambda: clock[0])
    result = runner.invoke(cli_app, ["task", "t-1", "--wait", "--interval", "1", "--timeout", "3"])
    assert result.exit_code == cli_main.EXIT_TASK_TIMEOUT
    assert "timed out after 3s; task t-1 is working" in result.output
    assert clock[0] <= 3.0


def test_wait_survives_an_unreachable_daemon(token, polls) -> None:
    polls.extend([httpx.ConnectError("boom"), _task("COMPLETED", "back")])
    result = runner.invoke(cli_app, ["task", "t-1", "--wait"])
    assert result.exit_code == 0, result.output
    assert "back" in result.output


def test_task_without_wait_reports_an_unreachable_daemon(token, polls) -> None:
    polls.append(httpx.ConnectError("boom"))
    result = runner.invoke(cli_app, ["task", "t-1"])
    assert result.exit_code == 1
    assert "could not reach daemon" in result.output


def test_task_not_found_is_a_clear_error(token, monkeypatch) -> None:
    def missing(**kw):
        raise api.A2AError(-32001, "Task not found")

    monkeypatch.setattr(api, "get_task", missing)
    result = runner.invoke(cli_app, ["task", "nope", "--target", "echo"])
    assert result.exit_code == 1
    assert "Task not found" in result.output


def test_task_without_token_errors(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", tmp_path / "absent")
    result = runner.invoke(cli_app, ["task", "t-1"])
    assert result.exit_code == 1
    assert "no token" in result.output.lower()


def test_wire_requests(monkeypatch) -> None:
    """The client sends v1.0 SendMessage with returnImmediately, then GetTask."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        result = {"task": _task("SUBMITTED")} if body["method"] == "SendMessage" else _task("WORKING")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real = httpx.Client
    monkeypatch.setattr(
        api, "make_client", lambda **kw: real(base_url="http://d", transport=httpx.MockTransport(handler))
    )
    task = api.dispatch_detached(token="tok", target="echo", message_text="hi", context_id="c-9")
    assert task["id"] == "t-1"
    api.get_task(token="tok", target="echo", task_id="t-1")
    send, get = (json.loads(r.content) for r in seen)
    assert seen[0].url.path == "/agents/echo/a2a"
    assert seen[0].headers["A2A-Version"] == "1.0"
    assert seen[0].headers["Authorization"] == "Bearer tok"
    assert send["params"]["configuration"] == {"returnImmediately": True}
    assert send["params"]["message"]["contextId"] == "c-9"
    assert send["params"]["message"]["parts"] == [{"text": "hi"}]
    assert get["method"] == "GetTask" and get["params"] == {"id": "t-1"}


def _mock_admin(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.Client
    monkeypatch.setattr(
        api, "make_client", lambda **kw: real(base_url="http://d", transport=httpx.MockTransport(record))
    )
    return seen


def test_task_target_reads_the_conversations_target_agent(monkeypatch) -> None:
    """The persona lookup, as a unit test: the integration test was its only cover (#393)."""
    seen = _mock_admin(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"conversation": {"target_agent": "echo", "caller_agent": "operator"}}
        ),
    )
    assert api.task_target(token="tok", task_id="t-9") == "echo"
    assert seen[0].url.path == "/admin/conversations/t-9"
    assert seen[0].headers["Authorization"] == "Bearer tok"


@pytest.fixture
def token_only(monkeypatch, tmp_path):
    """A saved token, with the real task_target (the `token` fixture stubs it)."""
    path = tmp_path / "token"
    path.write_text("tok")
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", path)


def test_wait_retries_a_lookup_against_a_down_daemon(token_only, polls, monkeypatch) -> None:
    looks: list[object] = [httpx.ConnectError("down"), "echo"]

    def lookup(**kw):
        item = looks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(api, "task_target", lookup)
    polls.append(_task("COMPLETED", "done"))
    result = runner.invoke(cli_app, ["task", "t-1", "--wait"])
    assert result.exit_code == 0, result.output
    assert "retrying" in result.output and "done" in result.output
    assert polls.sleeps == [2.0]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_lookup_names_target(token_only, monkeypatch, status) -> None:
    _mock_admin(monkeypatch, lambda r: httpx.Response(status, json={"detail": "requires operator scope"}))
    result = runner.invoke(cli_app, ["task", "t-1"])
    assert result.exit_code == 1
    assert "task lookup refused: requires operator scope" in result.output
    assert "--target <persona>" in result.output


def test_a_lookup_404_is_not_given_the_target_hint(token_only, monkeypatch) -> None:
    _mock_admin(monkeypatch, lambda r: httpx.Response(404, json={"detail": "no such conversation"}))
    result = runner.invoke(cli_app, ["task", "t-1"])
    assert result.exit_code == 1
    assert "no such conversation" in result.output and "--target" not in result.output


def test_cancel_with_a_refused_lookup_names_target(token_only, monkeypatch) -> None:
    _mock_admin(monkeypatch, lambda r: httpx.Response(403, json={"detail": "invalid_audience"}))
    result = runner.invoke(cli_app, ["task", "t-1", "--cancel"])
    assert result.exit_code == 1 and "--target <persona>" in result.output


def test_wait_polls_on_the_deadline_then_stops(token, polls, monkeypatch) -> None:
    """The last sleep is cut to the time left, so the final poll lands on the
    deadline rather than up to an interval before it (#393)."""
    polls.extend([_task("WORKING")] * 20)
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(cli_main.time, "sleep", sleep)
    monkeypatch.setattr(cli_main.time, "monotonic", lambda: clock[0])
    result = runner.invoke(cli_app, ["task", "t-1", "--wait", "--interval", "2", "--timeout", "5"])
    assert result.exit_code == cli_main.EXIT_TASK_TIMEOUT
    assert sleeps == [2.0, 2.0, 1.0]
    assert clock[0] == 5.0


# ---- against a live daemon ---------------------------------------------------

SLOW_S = 1.5


class _Stub:
    """Stands in for echo: sleeps SLOW_S, then answers or fails."""

    name = "echo"

    async def handle_task(self, task: Task) -> Task:
        await asyncio.sleep(SLOW_S)
        assert task.status.message is not None
        text = task.status.message.parts[0].text  # type: ignore[union-attr]
        if text == "fail":
            raise RuntimeError("stub peer failed")
        return Task(
            id=task.id,
            contextId=task.context_id,
            status=TaskStatus(
                state="completed",
                message=Message(role="agent", parts=[TextPart(text=f"slow: {text}")]),
            ),
            history=[],
            metadata=task.metadata,
        )


def _last(result) -> str:
    """The command's own line: an in-process daemon logs to the same stdout."""
    return result.output.strip().splitlines()[-1]


@pytest.mark.integration  # real uvicorn daemon, like test_cli_login_dispatch
def test_detached_dispatch_against_a_slow_peer(monkeypatch, tmp_path) -> None:
    db_url = os.environ["CABAL_DB_URL"]
    daemon = create_app(
        agent="all", config_path="identity.toml", db_url=db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    )
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", tmp_path / "token")
    with serve(daemon) as port:
        daemon.state.dispatch_router.register_local(_Stub())
        with contextlib.closing(sqlite3.connect(db_url.removeprefix("sqlite:///"))) as db:
            (code,) = db.execute("SELECT code FROM bootstrap_codes WHERE used_at IS NULL").fetchone()
        monkeypatch.setenv("CABAL_URL", f"http://127.0.0.1:{port}")
        assert runner.invoke(cli_app, ["login", "--code", code]).exit_code == 0

        started = time.monotonic()
        result = runner.invoke(cli_app, ["dispatch", "--detach", "echo", "hi"])
        assert result.exit_code == 0, result.output
        assert time.monotonic() - started < SLOW_S  # returned before the peer answered
        task_id, state = _last(result).split()
        assert state == "[submitted]"

        # Not finished yet, and `cbl task` says so without waiting.
        early = runner.invoke(cli_app, ["task", task_id])
        assert early.exit_code == 0 and "completed" not in _last(early)

        waited = runner.invoke(cli_app, ["task", task_id, "--wait", "--interval", "0.2"])
        assert waited.exit_code == 0, waited.output
        assert _last(waited) == "[completed] slow: hi"

        failing = runner.invoke(cli_app, ["dispatch", "--detach", "echo", "fail"])
        failed_id = _last(failing).split()[0]
        result = runner.invoke(cli_app, ["task", failed_id, "--wait", "--interval", "0.2"])
        assert result.exit_code == 1
        assert _last(result).startswith("[failed]")

        slow = runner.invoke(cli_app, ["dispatch", "--detach", "echo", "hi"])
        result = runner.invoke(
            cli_app, ["task", _last(slow).split()[0], "--wait", "--interval", "0.1", "--timeout", "0.3"]
        )
        assert result.exit_code == cli_main.EXIT_TASK_TIMEOUT
        assert "timed out" in result.output


# ---- CABAL_DISPATCH_TIMEOUT_S on a detached task's federated call (issue #391) ----


async def _peer(delay_s: float):
    """A federated peer that takes `delay_s` to answer a dispatch."""

    async def serve_one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = int(next(
            line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")
        ))
        sent = json.loads(await reader.readexactly(length))
        await asyncio.sleep(delay_s)
        body = json.dumps(
            {
                "id": sent["id"],
                "status": {"state": "completed", "message": {"role": "agent", "parts": [{"kind": "text", "text": "peer says hi"}]}},
            }
        ).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve_one, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _detached_peer_task(monkeypatch, *, timeout_s: str, peer_delay_s: float) -> str:
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", timeout_s)
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    daemon: FastAPI = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    server, port = await _peer(peer_delay_s)
    try:
        async with daemon.router.lifespan_context(daemon):
            async with open_session(daemon.state.sessionmaker) as session:
                session.add(Persona(name="scout", config_json={}))
                await session.flush()
                await bulletin_store.register_federated(
                    session,
                    name="scout",
                    persona_name="scout",
                    endpoint=f"http://127.0.0.1:{port}",
                    federation_audience="scout",
                    agent_card_json={"name": "scout"},
                    agent_card_kid=daemon.state.active_kid,
                )
                await session.commit()
                stored = await load_active_signing_key(session, master_key=daemon.state.master_key)
            assert stored is not None
            tok = mint(signing_key=stored, sub="agent:alice", aud="cabal-core", scope=["colleague"])
            headers = {"A2A-Version": "1.0", "Authorization": f"Bearer {tok}"}
            async with AsyncClient(transport=httpx.ASGITransport(app=daemon), base_url="http://test") as c:

                async def call(method: str, params: dict) -> dict:
                    r = await c.post(
                        "/agents/scout/a2a",
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                        headers=headers,
                    )
                    return r.json()["result"]

                sent = await call(
                    "SendMessage",
                    {
                        "message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "go"}]},
                        "configuration": {"returnImmediately": True},
                    },
                )
                task_id = sent["task"]["id"]
                for _ in range(300):
                    state = (await call("GetTask", {"id": task_id}))["status"]["state"]
                    if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
                        return state
                    await asyncio.sleep(0.05)
                pytest.fail("detached task never ended")
    finally:
        server.close()
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_detached_peer_call_honours_the_dispatch_timeout(monkeypatch) -> None:
    """The run phase of a detached task calls a federated peer through the same
    http client as a blocking dispatch, so CABAL_DISPATCH_TIMEOUT_S bounds it:
    a peer slower than the ceiling fails the task, one inside it completes."""
    assert await _detached_peer_task(monkeypatch, timeout_s="0.3", peer_delay_s=1.0) == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_detached_peer_call_completes_inside_a_raised_dispatch_timeout(monkeypatch) -> None:
    assert await _detached_peer_task(monkeypatch, timeout_s="5", peer_delay_s=1.0) == "TASK_STATE_COMPLETED"
