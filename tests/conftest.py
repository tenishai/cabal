"""Shared fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cabal.core.app import create_app


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path: Path) -> None:
    """Each test gets a fresh temp dir for data/ and a generated master key."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("CABAL_MASTER_KEY", Fernet.generate_key().decode())
    db_file = tmp_path / "data" / "cabal.db"
    monkeypatch.setenv("CABAL_DB_URL", f"sqlite:///{db_file}")


@pytest_asyncio.fixture
async def app() -> FastAPI:
    # async db url for the running app; the sync env var above is what
    # alembic uses inside lifespan.
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    fastapi_app = create_app(
        agent="all", config_path="identity.toml", db_url=db_url
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
