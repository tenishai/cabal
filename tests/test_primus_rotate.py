"""primus rotate (admin endpoint + bulletin schema) tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.bulletin import store as bulletin_store
from cabal.core.models import Agent, BootstrapCode, Persona


async def _bootstrap_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _seed_rook_primus(app: FastAPI) -> None:
    async with app.state.sessionmaker() as s:
        s.add(Persona(name="rook", config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name="rook-primus",
            persona_name="rook",
            role="primus",
            endpoint="local://rook-primus",
            capabilities={"skills": []},
            safety_tier="strict",
            agent_card_json={"name": "rook-primus", "generation": 1},
            agent_card_kid=app.state.active_kid,
        )
        await s.commit()


@pytest.mark.asyncio
async def test_rotate_creates_new_generation_marks_old_draining(
    app: FastAPI, client: AsyncClient
) -> None:
    await _seed_rook_primus(app)
    token = await _bootstrap_token(app, client)
    r = await client.post(
        "/admin/primus/rotate",
        json={"persona": "rook"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["previous_generation"] == 1
    assert body["new_generation"] == 2
    assert body["agent"] == "rook-primus"

    async with app.state.sessionmaker() as s:
        rows = (
            await s.execute(
                select(Agent).where(Agent.name == "rook-primus").order_by(Agent.generation)
            )
        ).scalars().all()
        assert len(rows) == 2
        assert rows[0].status == "draining"
        assert rows[0].drain_started_at is not None
        assert rows[1].status == "active"
        assert rows[1].generation == 2


@pytest.mark.asyncio
async def test_rotate_accepts_full_agent_name(app: FastAPI, client: AsyncClient) -> None:
    await _seed_rook_primus(app)
    token = await _bootstrap_token(app, client)
    r = await client.post(
        "/admin/primus/rotate",
        json={"persona": "rook-primus"},  # full name also accepted
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_rotate_404_when_persona_unknown(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _bootstrap_token(app, client)
    r = await client.post(
        "/admin/primus/rotate",
        json={"persona": "ghost"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_rotate_requires_operator_scope(
    app: FastAPI, client: AsyncClient
) -> None:
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    await _seed_rook_primus(app)
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    assert stored is not None
    persona_token = mint(
        signing_key=stored,
        sub="persona:archivist",
        aud="cabal-core",
        scope=["dispatch"],
    )
    r = await client.post(
        "/admin/primus/rotate",
        json={"persona": "rook"},
        headers={"Authorization": f"Bearer {persona_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_rotation_preserves_subordinate_routing(
    app: FastAPI, client: AsyncClient
) -> None:
    """After rotation, a subordinate-style return targeting generation=1
    can still resolve the draining row by exact (name, generation) lookup."""
    await _seed_rook_primus(app)
    token = await _bootstrap_token(app, client)
    await client.post(
        "/admin/primus/rotate",
        json={"persona": "rook"},
        headers={"Authorization": f"Bearer {token}"},
    )

    async with app.state.sessionmaker() as s:
        gen1 = await bulletin_store.get(s, "rook-primus", 1)
        gen2 = await bulletin_store.get(s, "rook-primus", 2)
    assert gen1 is not None and gen1.status == "draining"
    assert gen2 is not None and gen2.status == "active"
    assert gen1.endpoint == gen2.endpoint  # routing still works
