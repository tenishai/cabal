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
