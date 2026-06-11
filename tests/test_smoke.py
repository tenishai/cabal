"""Smoke tests: app starts, version is exposed, routes respond."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import cabal


def test_version_exposed() -> None:
    assert cabal.__version__
    assert isinstance(cabal.__version__, str)


@pytest.mark.asyncio
async def test_healthz_responds(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == cabal.__version__


@pytest.mark.asyncio
async def test_agent_card_stub(client: AsyncClient) -> None:
    resp = await client.get("/.well-known/agent-card")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "cabal-core"


@pytest.mark.asyncio
async def test_jwks_stub(client: AsyncClient) -> None:
    resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    assert "keys" in resp.json()


def test_cli_version_via_runner() -> None:
    """`cbl version` prints something — uses Typer's testing CliRunner."""
    from typer.testing import CliRunner

    from cabal.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "cabal" in result.stdout.lower()
