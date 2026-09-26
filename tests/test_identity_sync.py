"""identity_sync tests."""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.config.identity import IdentityConfig, PersonaConfig
from cabal.core.auth.keys import create_signing_key
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.identity_sync import sync_personas
from cabal.core.models import Agent, Base, Persona


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest_asyncio.fixture
async def signing_key(sessionmaker):
    master = Fernet.generate_key()
    async with sessionmaker() as s:
        stored = await create_signing_key(s, master_key=master)
        await s.commit()
    return stored


def _identity(**personas: PersonaConfig) -> IdentityConfig:
    cfg = IdentityConfig()
    cfg.personas = personas
    return cfg


@pytest.mark.asyncio
async def test_primus_registered_as_persona_minus_primus(
    sessionmaker, signing_key
) -> None:
    cfg = _identity(
        mason=PersonaConfig(
            description="Engineering", role="primus", safety_tier="strict"
        )
    )
    await sync_personas(sessionmaker, cfg, signing_key)
    async with sessionmaker() as s:
        personas = (await s.execute(select(Persona))).scalars().all()
        agents = (await s.execute(select(Agent))).scalars().all()
        assert [p.name for p in personas] == ["mason"]
        assert [a.name for a in agents] == ["mason-primus"]
        assert agents[0].generation == 1
        assert agents[0].status == "active"
        assert agents[0].safety_tier == "strict"
        assert agents[0].agent_card_kid == signing_key.kid


@pytest.mark.asyncio
async def test_singleton_registered_under_persona_name(
    sessionmaker, signing_key
) -> None:
    cfg = _identity(
        archivist=PersonaConfig(role="singleton", safety_tier="moderate")
    )
    await sync_personas(sessionmaker, cfg, signing_key)
    async with sessionmaker() as s:
        agents = (await s.execute(select(Agent))).scalars().all()
        assert [a.name for a in agents] == ["archivist"]
        assert agents[0].safety_tier == "moderate"


@pytest.mark.asyncio
async def test_sync_is_idempotent(sessionmaker, signing_key) -> None:
    """Running sync twice doesn't duplicate persona rows or agent rows."""
    cfg = _identity(
        mason=PersonaConfig(role="primus", safety_tier="strict"),
        archivist=PersonaConfig(role="singleton", safety_tier="moderate"),
    )
    await sync_personas(sessionmaker, cfg, signing_key)
    await sync_personas(sessionmaker, cfg, signing_key)
    async with sessionmaker() as s:
        assert len((await s.execute(select(Persona))).scalars().all()) == 2
        assert len((await s.execute(select(Agent))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_signed_card_stored_in_agents_row(
    sessionmaker, signing_key
) -> None:
    """The agent_card_json is a JWS compact + parsed card dict."""
    cfg = _identity(
        mason=PersonaConfig(role="primus", safety_tier="strict")
    )
    await sync_personas(sessionmaker, cfg, signing_key)
    async with sessionmaker() as s:
        row = (await s.execute(select(Agent))).scalar_one()
        assert "jws" in row.agent_card_json
        assert row.agent_card_json["jws"].count(".") == 2  # JWS compact
        assert row.agent_card_json["card"]["name"] == "mason-primus"
        assert (
            row.agent_card_json["card"]["metadata"]["cabal_persona"] == "mason"
        )


@pytest.mark.asyncio
async def test_resync_after_graduate_then_absorb_takes_the_next_generation(
    sessionmaker, signing_key
) -> None:
    """cabal#149 — the documented recovery from absorb must not collide.

    sync (gen 1) -> graduate (retire 1, federated gen 2) -> absorb (retire 2,
    no successor) -> `cbl reload identity` -> sync. `get_active` sees no active
    row, and a hardcoded generation 1 hit the retired row on the PK:
    `UNIQUE constraint failed: agents.name, agents.generation`.
    """
    from cabal.core.bulletin import store as bulletin_store

    cfg = _identity(archivist=PersonaConfig(role="singleton"))
    await sync_personas(sessionmaker, cfg, signing_key)
    async with sessionmaker() as s:
        # graduate, as admin.persona_graduate does it
        await bulletin_store.mark_retired(s, "archivist", 1)
        fed = await bulletin_store.register_federated(
            s,
            name="archivist",
            persona_name="archivist",
            endpoint="https://archivist.example",
            federation_audience="archivist@peer",
            agent_card_json={},
            agent_card_kid="",
        )
        assert fed.generation == 2
        # absorb retires the federated row without a successor
        await bulletin_store.mark_retired(s, "archivist", 2)
        await s.commit()

    await sync_personas(sessionmaker, cfg, signing_key)

    async with sessionmaker() as s:
        active = await bulletin_store.get_active(s, "archivist")
    assert active is not None
    assert active.generation == 3
    assert active.kind == "member"
    # The signed card names the same generation as the row.
    assert active.agent_card_json["card"]["metadata"]["cabal_generation"] == 3


@pytest.mark.asyncio
async def test_register_agent_without_a_generation_never_reuses_a_retired_one(
    sessionmaker,
) -> None:
    """cabal#149 — the default itself, not one caller. Omitting `generation`
    resolves MAX+1 across all statuses, so every current and future caller
    that omits it (sync_personas, _ensure_echo_agent, register_federated,
    register_session) is safe after a retire."""
    from cabal.core.bulletin import store as bulletin_store

    async with sessionmaker() as s:
        s.add(Persona(name="echo", config_json={}))
        await s.flush()
        kw = dict(
            name="echo",
            persona_name="echo",
            role="builtin",
            endpoint="local://echo",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="",
        )
        first = await bulletin_store.register_agent(s, **kw)
        assert first.generation == 1
        await bulletin_store.mark_retired(s, "echo", 1)
        second = await bulletin_store.register_agent(s, **kw)
        assert second.generation == 2
