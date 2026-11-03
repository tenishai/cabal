"""`cbl dispatch --metadata` (tenishai/agents#171): a caller names per-task values, such
as a daedalus bundle's workspace and review target, and they reach the agent unchanged
on both the waiting and the --detach paths."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3

import pytest
from typer.testing import CliRunner

import cabal.cli.client as api
from cabal.cli.main import app as cli_app
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import create_app
from tests.test_cli_detach import _last
from tests.test_cli_login_dispatch import serve

runner = CliRunner()
META = {"daedalus_workspace": "r-1", "review": {"repo": "o/r", "pr": "7", "sha": "a" * 40}}


@pytest.fixture
def token(monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text("tok")
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", path)


def _done(**kw) -> dict:
    return {"id": "t-1", "status": {"state": "completed", "message": {"parts": []}}}


@pytest.mark.parametrize("detach", [False, True])
def test_metadata_is_passed_to_the_client(token, monkeypatch, detach) -> None:
    calls: list[dict] = []
    name = "dispatch_detached" if detach else "dispatch"
    monkeypatch.setattr(api, name, lambda **kw: calls.append(kw) or _done())
    args = ["dispatch", *(["--detach"] if detach else []), "echo", "hi", "--metadata",
            '{"daedalus_workspace": "r-1", "review": {"pr": "7"}}']
    result = runner.invoke(cli_app, args)
    assert result.exit_code == 0, result.output
    assert calls[0]["metadata"] == {"daedalus_workspace": "r-1", "review": {"pr": "7"}}


@pytest.mark.parametrize("detach", [False, True])
def test_no_flag_sends_no_metadata(token, monkeypatch, detach) -> None:
    calls: list[dict] = []
    name = "dispatch_detached" if detach else "dispatch"
    monkeypatch.setattr(api, name, lambda **kw: calls.append(kw) or _done())
    runner.invoke(cli_app, ["dispatch", *(["--detach"] if detach else []), "echo", "hi"])
    assert "metadata" not in calls[0]


@pytest.mark.parametrize("raw, why", [("not json", "not JSON"), ("[1]", "must be a JSON object")])
def test_metadata_that_is_not_a_json_object_is_refused_before_any_request(
    token, monkeypatch, raw, why
) -> None:
    def never(**kw):
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(api, "dispatch", never)
    result = runner.invoke(cli_app, ["dispatch", "echo", "hi", "--metadata", raw])
    assert result.exit_code == 2
    assert why in result.output


def test_detached_metadata_goes_in_the_send_message_request(monkeypatch) -> None:
    sent: list[dict] = []

    def fake(*, token, target, method, params):
        sent.append(params)
        return {"task": {"id": "t-1", "status": {"state": "TASK_STATE_SUBMITTED"}}}

    monkeypatch.setattr(api, "_a2a_v1", fake)
    api.dispatch_detached(token="tok", target="echo", message_text="hi", metadata=META)
    assert sent[0]["metadata"] == META
    api.dispatch_detached(token="tok", target="echo", message_text="hi")
    assert "metadata" not in sent[1]


class _Recorder:
    """Stands in for echo and records the metadata each task arrived with."""

    name = "echo"

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def handle_task(self, task: Task) -> Task:
        self.seen.append(dict(task.metadata))
        return Task(
            id=task.id,
            contextId=task.context_id,
            status=TaskStatus(state="completed", message=Message(role="agent", parts=[TextPart(text="ok")])),
            history=[],
            metadata=task.metadata,
        )


@pytest.mark.integration  # real uvicorn daemon, like test_cli_login_dispatch
def test_metadata_reaches_the_agent_on_both_paths(monkeypatch, tmp_path) -> None:
    db_url = os.environ["CABAL_DB_URL"]
    daemon = create_app(
        agent="all", config_path="identity.toml", db_url=db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    )
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", tmp_path / "token")
    recorder = _Recorder()
    with serve(daemon) as port:
        daemon.state.dispatch_router.register_local(recorder)
        with contextlib.closing(sqlite3.connect(db_url.removeprefix("sqlite:///"))) as db:
            (code,) = db.execute("SELECT code FROM bootstrap_codes WHERE used_at IS NULL").fetchone()
        monkeypatch.setenv("CABAL_URL", f"http://127.0.0.1:{port}")
        assert runner.invoke(cli_app, ["login", "--code", code]).exit_code == 0

        meta = json.dumps(META)
        result = runner.invoke(cli_app, ["dispatch", "echo", "hi", "--metadata", meta])
        assert result.exit_code == 0, result.output

        detached = runner.invoke(cli_app, ["dispatch", "--detach", "echo", "hi", "--metadata", meta])
        assert detached.exit_code == 0, detached.output
        waited = runner.invoke(cli_app, ["task", _last(detached).split()[0], "--wait", "--interval", "0.2"])
        assert waited.exit_code == 0, waited.output

    assert len(recorder.seen) == 2
    for seen in recorder.seen:
        assert {k: seen[k] for k in META} == META
