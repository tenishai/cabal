"""Tests for cbl persona graduate / absorb admin endpoints (cabal#45).

Tests cover:
1. Graduate — row becomes kind=federated, local handler is unregistered.
2. Absorb  — federated row retired, local re-registration happens on identity
             reload (tested separately via the bulletin row state only here).
3. Error paths: graduate with bad args, absorb on non-federated agent.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.bulletin import store as bulletin_store
from cabal.core.models import Agent, BootstrapCode, Persona

# ---- helpers -------------------------------------------------------


async def _bootstrap_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _seed_archivist_local(app: FastAPI) -> None:
    """Seed a local member row for archivist and register a fake local handler."""
    async with app.state.sessionmaker() as s:
        s.add(Persona(name="archivist", config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name="archivist",
            persona_name="archivist",
            role="singleton",
            endpoint="local://archivist",
            capabilities={},
            safety_tier="strict",
            agent_card_json={"name": "archivist", "generation": 1},
            agent_card_kid=app.state.active_kid,
        )
        await s.commit()

    # Simulate a local handler being registered (e.g. from persona factory).
    class _FakeHandler:
        name = "archivist"

    app.state.dispatch_router.register_local(_FakeHandler())


async def _seed_archivist_federated(app: FastAPI) -> None:
    """Seed a federated row for archivist (as if already graduated)."""
    async with app.state.sessionmaker() as s:
        s.add(Persona(name="archivist", config_json={}))
        await s.flush()
        await bulletin_store.register_federated(
            s,
            name="archivist",
            persona_name="archivist",
            endpoint="https://archivist.rd.ts.net",
            federation_audience="archivist@rd",
            agent_card_json={},
            agent_card_kid="",
        )
        await s.commit()


# ---- graduate ----------------------------------------------------------


@pytest.mark.asyncio
async def test_graduate_registers_federated_row(app: FastAPI, client: AsyncClient) -> None:
    """POST /admin/persona/{name}/graduate writes a kind=federated bulletin row."""
    await _seed_archivist_local(app)
    token = await _bootstrap_token(app, client)

    r = await client.post(
        "/admin/persona/archivist/graduate",
        json={
            "endpoint": "https://archivist.rd.ts.net",
            "audience": "archivist@rd",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["graduated"] is True
    assert body["agent"] == "archivist"
    assert body["endpoint"] == "https://archivist.rd.ts.net"
    assert body["audience"] == "archivist@rd"
    assert body["previous_generation_retired"] == 1

    # The active bulletin row must now be federated.
    async with app.state.sessionmaker() as s:
        active = await bulletin_store.get_active(s, "archivist")
    assert active is not None
    assert active.kind == "federated"
    assert active.federation_audience == "archivist@rd"
    assert active.endpoint == "https://archivist.rd.ts.net"


@pytest.mark.asyncio
async def test_graduate_retires_existing_local_row(app: FastAPI, client: AsyncClient) -> None:
    """Graduate retires the previous local row (gen 1 → retired)."""
    await _seed_archivist_local(app)
    token = await _bootstrap_token(app, client)

    await client.post(
        "/admin/persona/archivist/graduate",
        json={"endpoint": "https://archivist.rd.ts.net", "audience": "archivist@rd"},
        headers={"Authorization": f"Bearer {token}"},
    )

    async with app.state.sessionmaker() as s:
        rows = (
            await s.execute(
                select(Agent)
                .where(Agent.name == "archivist")
                .order_by(Agent.generation)
            )
        ).scalars().all()

    # Gen 1 must be retired; gen 2 active + federated.
    assert len(rows) == 2
    assert rows[0].status == "retired"
    assert rows[1].status == "active"
    assert rows[1].kind == "federated"
    assert rows[1].generation == 2


@pytest.mark.asyncio
async def test_graduate_unregisters_local_handler(app: FastAPI, client: AsyncClient) -> None:
    """Graduate drops the in-process local handler from the dispatch router."""
    await _seed_archivist_local(app)
    assert app.state.dispatch_router.has_local("archivist"), "precondition: local handler must exist"

    token = await _bootstrap_token(app, client)
    await client.post(
        "/admin/persona/archivist/graduate",
        json={"endpoint": "https://archivist.rd.ts.net", "audience": "archivist@rd"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Local handler must be gone after graduate.
    assert not app.state.dispatch_router.has_local("archivist")


@pytest.mark.asyncio
async def test_graduate_works_without_existing_row(app: FastAPI, client: AsyncClient) -> None:
    """Graduate on a name with no existing bulletin row creates a gen-1 federated row."""
    async with app.state.sessionmaker() as s:
        s.add(Persona(name="librarian", config_json={}))
        await s.commit()

    token = await _bootstrap_token(app, client)
    r = await client.post(
        "/admin/persona/librarian/graduate",
        json={"endpoint": "https://librarian.rd.ts.net", "audience": "librarian@rd"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generation"] == 1
    assert body["previous_generation_retired"] is None


@pytest.mark.asyncio
async def test_graduate_without_persona_or_row(app: FastAPI, client: AsyncClient) -> None:
    """Client-cabal case: graduate a name with NO Persona and NO bulletin row.

    `agents.persona_name` FKs to `personas.name`, so register_federated would
    FK-fail unless the endpoint upserts the Persona first. Regression guard for
    the opaque-500 the raw insert produced (federated-bulletin-and-router §3).
    """
    token = await _bootstrap_token(app, client)
    r = await client.post(
        "/admin/persona/scribe/graduate",
        json={"endpoint": "https://scribe.rd.ts.net", "audience": "scribe@rd"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generation"] == 1
    assert body["previous_generation_retired"] is None

    # The federated row must be persisted with kind=federated, and the Persona
    # placeholder must now exist to satisfy the FK.
    async with app.state.sessionmaker() as s:
        row = await bulletin_store.get_active(s, "scribe")
        assert row is not None
        assert row.kind == "federated"
        assert await s.get(Persona, "scribe") is not None


@pytest.mark.asyncio
async def test_graduate_400_on_missing_fields(app: FastAPI, client: AsyncClient) -> None:
    """Graduate with empty endpoint/audience returns 400."""
    await _seed_archivist_local(app)
    token = await _bootstrap_token(app, client)

    r = await client.post(
        "/admin/persona/archivist/graduate",
        json={"endpoint": "", "audience": "archivist@rd"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_graduate_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    """A non-operator token is rejected with 401/403."""
    from cabal.core.auth.jwt import mint as _mint

    async with app.state.sessionmaker() as s:
        from cabal.core.auth.keys import load_active_signing_key
        stored = await load_active_signing_key(s, master_key=app.state.master_key)
    limited = _mint(signing_key=stored, sub="guest", aud="cabal-core", scope=["dispatch"])
    r = await client.post(
        "/admin/persona/archivist/graduate",
        json={"endpoint": "https://archivist.rd.ts.net", "audience": "archivist@rd"},
        headers={"Authorization": f"Bearer {limited}"},
    )
    assert r.status_code in {401, 403}


# ---- absorb ------------------------------------------------------------


@pytest.mark.asyncio
async def test_absorb_retires_federated_row(app: FastAPI, client: AsyncClient) -> None:
    """POST /admin/persona/{name}/absorb retires the active federated row."""
    await _seed_archivist_federated(app)
    token = await _bootstrap_token(app, client)

    r = await client.post(
        "/admin/persona/archivist/absorb",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["absorbed"] is True
    assert body["agent"] == "archivist"
    assert body["retired_generation"] == 1

    # The federated row must now be retired; no active row.
    async with app.state.sessionmaker() as s:
        active = await bulletin_store.get_active(s, "archivist")
    assert active is None

    # Raw row is retired.
    async with app.state.sessionmaker() as s:
        raw = (
            await s.execute(
                select(Agent).where(Agent.name == "archivist", Agent.generation == 1)
            )
        ).scalar_one()
    assert raw.status == "retired"


@pytest.mark.asyncio
async def test_absorb_404_when_no_active_row(app: FastAPI, client: AsyncClient) -> None:
    """Absorb on an unknown agent returns 404."""
    token = await _bootstrap_token(app, client)
    r = await client.post(
        "/admin/persona/ghost/absorb",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_absorb_409_when_row_is_local(app: FastAPI, client: AsyncClient) -> None:
    """Absorb on a non-federated (local member) agent returns 409."""
    await _seed_archivist_local(app)
    token = await _bootstrap_token(app, client)

    r = await client.post(
        "/admin/persona/archivist/absorb",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_absorb_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    """A non-operator token is rejected for absorb too."""
    from cabal.core.auth.jwt import mint as _mint

    async with app.state.sessionmaker() as s:
        from cabal.core.auth.keys import load_active_signing_key
        stored = await load_active_signing_key(s, master_key=app.state.master_key)
    limited = _mint(signing_key=stored, sub="guest", aud="cabal-core", scope=["dispatch"])
    r = await client.post(
        "/admin/persona/archivist/absorb",
        headers={"Authorization": f"Bearer {limited}"},
    )
    assert r.status_code in {401, 403}


# ---- round-trip: graduate then absorb ----------------------------------


@pytest.mark.asyncio
async def test_graduate_then_absorb_round_trip(app: FastAPI, client: AsyncClient) -> None:
    """Graduate followed by absorb leaves the bulletin with no active row.

    The local handler would be restored by `cbl reload identity`; here we
    verify only that the row lifecycle is correct.
    """
    await _seed_archivist_local(app)
    token = await _bootstrap_token(app, client)

    # Graduate.
    r = await client.post(
        "/admin/persona/archivist/graduate",
        json={"endpoint": "https://archivist.rd.ts.net", "audience": "archivist@rd"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text

    # Verify federated row is active.
    async with app.state.sessionmaker() as s:
        row = await bulletin_store.get_active(s, "archivist")
    assert row is not None and row.kind == "federated"

    # Absorb.
    r = await client.post(
        "/admin/persona/archivist/absorb",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text

    # No active row after absorb.
    async with app.state.sessionmaker() as s:
        row = await bulletin_store.get_active(s, "archivist")
    assert row is None
