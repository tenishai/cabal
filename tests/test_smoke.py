"""Smoke tests: app starts, version is exposed, routes respond."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import cabal


def test_version_exposed() -> None:
    assert cabal.__version__
    assert isinstance(cabal.__version__, str)


def test_version_lookup_uses_the_distribution_name() -> None:
    """__version__ reads importlib.metadata by distribution name, and falls
    back to 0.0.0+unknown when that name isn't installed, so a rename that
    misses cabal/__init__.py would pass test_version_exposed. Tie the two."""
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    name = tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"]
    source = (root / "src" / "cabal" / "__init__.py").read_text()
    assert re.search(r'version\(\s*["\']' + re.escape(name) + r'["\']\s*\)', source), name


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
async def test_jwks_returns_bootstrapped_key(client: AsyncClient) -> None:
    resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    body = resp.json()
    keys = body["keys"]
    assert len(keys) == 1
    key = keys[0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["kid"].startswith("cabal-")
    assert key["n"]  # non-empty modulus
    assert key["e"]  # non-empty exponent


@pytest.mark.asyncio
async def test_agent_card_carries_active_kid(client: AsyncClient) -> None:
    resp = await client.get("/.well-known/agent-card")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kid"].startswith("cabal-")


def test_cli_version_via_runner() -> None:
    """`cbl version` prints something — uses Typer's testing CliRunner."""
    from typer.testing import CliRunner

    from cabal.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "cabal" in result.stdout.lower()
