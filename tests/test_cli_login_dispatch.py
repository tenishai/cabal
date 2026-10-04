"""CLI login + dispatch tests against a live in-process daemon."""

from __future__ import annotations

import contextlib
import os
import socket
import sqlite3
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

import cabal.cli.client as api
from cabal.cli.main import app as cli_app
from cabal.core.app import create_app


@contextlib.contextmanager
def serve(fastapi_app: FastAPI):
    """Run uvicorn in a background thread bound to an ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait until the server is actually ACCEPTING connections — not just until
    # uvicorn flips `server.started`. On a slow/contended CI runner the app's
    # lifespan (alembic migrate + signing-key load + persona sync) can exceed a
    # short fixed wait, and `started` can flip before the listen socket accepts;
    # a too-short wait then yielded a port that refused the CLI's connection
    # (the historical flake). Poll real reachability with a generous ceiling and
    # fail loudly if it never comes up, so a genuine boot failure is diagnosable.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if server.started:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                pass
        time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError(f"uvicorn did not become reachable on 127.0.0.1:{port} within 30s")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.integration  # spins up a real uvicorn daemon — excluded from the unit job
def test_cli_login_and_dispatch_against_live_daemon(monkeypatch, tmp_path) -> None:
    """End-to-end: launch the FastAPI app over a real socket, drive it via
    the cbl CLI, verify login + dispatch + token storage.

    uvicorn runs the app's only lifespan. Using the `app` fixture here ran it
    twice against one SQLite file, and the fixture's first heartbeat could hold
    the write lock while serve() blocked its event loop (cabal#254)."""
    db_url = os.environ["CABAL_DB_URL"]
    app = create_app(
        agent="all",
        config_path="identity.toml",
        db_url=db_url.replace("sqlite://", "sqlite+aiosqlite://", 1),
    )
    runner = CliRunner()
    token_path = tmp_path / "token"
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", token_path)

    with serve(app) as port:
        # The lifespan has issued the bootstrap code by the time serve() yields.
        with contextlib.closing(sqlite3.connect(db_url.removeprefix("sqlite:///"))) as db:
            (code,) = db.execute(
                "SELECT code FROM bootstrap_codes WHERE used_at IS NULL"
            ).fetchone()
        monkeypatch.setenv("CABAL_URL", f"http://127.0.0.1:{port}")

        # cbl login --code <CODE>
        result = runner.invoke(cli_app, ["login", "--code", code])
        assert result.exit_code == 0, result.output
        assert "Logged in" in result.output
        assert token_path.exists()
        assert (token_path.stat().st_mode & 0o777) == 0o600

        # cbl dispatch echo "hi cabal"
        result = runner.invoke(cli_app, ["dispatch", "echo", "hi cabal"])
        assert result.exit_code == 0, result.output
        assert "[completed]" in result.output
        assert "echo: hi cabal" in result.output


def test_cli_dispatch_without_token_errors(monkeypatch, tmp_path) -> None:
    """`cbl dispatch` with no saved token exits with an actionable message."""
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", tmp_path / "absent")
    runner = CliRunner()
    result = runner.invoke(cli_app, ["dispatch", "echo", "hi"])
    assert result.exit_code == 1
    assert "no token" in result.output.lower()


def test_cli_dispatch_context_threads_a_context_id(monkeypatch, tmp_path) -> None:
    """cabal#161: `--context` is how a one-shot opts into a per-conversation
    delegation budget; without it no contextId is sent."""
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", token_path)
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"status": {"state": "completed", "message": {"parts": []}}}

    monkeypatch.setattr(api, "dispatch", fake_dispatch)
    runner = CliRunner()
    result = runner.invoke(cli_app, ["dispatch", "echo", "hi", "--context", "batch-1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_app, ["dispatch", "echo", "hi"])
    assert result.exit_code == 0, result.output
    assert [c["context_id"] for c in calls] == ["batch-1", None]


def test_the_token_follows_cabal_home(monkeypatch, tmp_path) -> None:
    """cabal#216: the token lives under CABAL_HOME like the rest of cabal's
    state, and keeps its old ~/.cabal/token home when CABAL_HOME is unset."""
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", None)
    monkeypatch.setenv("CABAL_HOME", str(tmp_path / "home-a"))
    assert api.TokenStore().path == tmp_path / "home-a" / "token"
    monkeypatch.delenv("CABAL_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert api.TokenStore().path == tmp_path / "user" / ".cabal" / "token"


def test_login_reports_the_path_it_saved_to(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", None)
    monkeypatch.setenv("CABAL_HOME", str(tmp_path / "home-b"))
    monkeypatch.setattr(api, "bootstrap", lambda code: {"access_token": "tok"})
    result = CliRunner().invoke(cli_app, ["login", "--code", "ABC123"])
    assert result.exit_code == 0, result.output
    saved = tmp_path / "home-b" / "token"
    assert saved.read_text() == "tok"
    assert str(saved) in result.output
