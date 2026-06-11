"""/admin/agents tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.models import BootstrapCode


async def _peek_bootstrap_code(app: FastAPI) -> str:
    async with app.state.sessionmaker() as s:
        result = await s.execute(
            select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
        )
        return result.scalar_one().code


@pytest.mark.asyncio
async def test_admin_agents_requires_operator_scope(
    app: FastAPI, client: AsyncClient
) -> None:
    # Build a non-operator token
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    assert stored is not None
    persona_token = mint(
        signing_key=stored,
        sub="persona:archivist",
        aud="archivist",
        scope=["dispatch"],
    )
    resp = await client.get(
        "/admin/agents", headers={"Authorization": f"Bearer {persona_token}"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_agents_lists_echo_for_operator(
    app: FastAPI, client: AsyncClient
) -> None:
    code = await _peek_bootstrap_code(app)
    r = await client.post("/auth/bootstrap", json={"code": code})
    token = r.json()["access_token"]

    resp = await client.get("/admin/agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    names = {a["name"] for a in body["agents"]}
    assert "echo" in names
    echo = next(a for a in body["agents"] if a["name"] == "echo")
    assert echo["status"] == "active"
    assert echo["role"] == "builtin"


@pytest.mark.asyncio
async def test_admin_agents_unauthenticated_rejected(client: AsyncClient) -> None:
    resp = await client.get("/admin/agents")
    assert resp.status_code == 401
