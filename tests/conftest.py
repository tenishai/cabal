"""Shared fixtures."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cabal.core.app import create_app


@pytest.fixture
def app() -> FastAPI:
    return create_app(agent="all", config_path="identity.toml")


@pytest.fixture
async def client(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
