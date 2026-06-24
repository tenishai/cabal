"""CLI login + dispatch tests against a live in-process daemon."""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from typer.testing import CliRunner

import cabal.cli.client as api
from cabal.cli.main import app as cli_app
from cabal.core.models import BootstrapCode


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


@pytest.mark.asyncio
async def test_cli_login_and_dispatch_against_live_daemon(
    app: FastAPI, monkeypatch, tmp_path
) -> None:
    """End-to-end: launch the FastAPI app over a real socket, drive it via
    the cbl CLI, verify login + dispatch + token storage."""
    # Fetch the bootstrap code from the same DB the app uses
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as s:
        result = await s.execute(
            select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
        )
        code = result.scalar_one().code

    # Start uvicorn against the configured app
    runner = CliRunner()
    token_path = tmp_path / "token"
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", token_path)

    with serve(app) as port:
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
