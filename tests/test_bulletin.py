"""Bulletin board CRUD tests."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.bulletin import store
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base, Persona


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


async def _seed_persona(s: AsyncSession, name: str = "rook") -> None:
    s.add(Persona(name=name, config_json={}))
    await s.flush()


@pytest.mark.asyncio
async def test_register_and_get_active(sessionmaker) -> None:
    async with sessionmaker() as s:
        await _seed_persona(s)
        registered = await store.register_agent(
            s,
            name="rook-primus",
            persona_name="rook",
            role="primus",
            endpoint="http://rook-primus:7400",
            capabilities={"skills": []},
            safety_tier="strict",
            agent_card_json={"name": "rook-primus"},
            agent_card_kid="kid-1",
        )
        await s.commit()
        assert registered.generation == 1
        assert registered.status == "active"

    async with sessionmaker() as s:
        active = await store.get_active(s, "rook-primus")
        assert active is not None
        assert active.endpoint == "http://rook-primus:7400"


@pytest.mark.asyncio
async def test_get_active_returns_none_when_only_draining(sessionmaker) -> None:
    async with sessionmaker() as s:
        await _seed_persona(s)
        await store.register_agent(
            s,
            name="rook-primus",
            persona_name="rook",
            role="primus",
            endpoint="http://rook-primus:7400",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
            status="draining",
        )
        await s.commit()

    async with sessionmaker() as s:
        assert await store.get_active(s, "rook-primus") is None


@pytest.mark.asyncio
async def test_rotation_flow(sessionmaker) -> None:
    """Mark old draining, insert new active; broker sees both via exact lookup
    and only the new via get_active."""
    async with sessionmaker() as s:
        await _seed_persona(s)
        await store.register_agent(
            s,
            name="rook-primus",
            persona_name="rook",
            role="primus",
            endpoint="http://rook-primus-g1:7400",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
            generation=1,
        )
        await s.commit()

    async with sessionmaker() as s:
        await store.mark_draining(s, "rook-primus", 1)
        await store.register_agent(
            s,
            name="rook-primus",
            persona_name="rook",
            role="primus",
            endpoint="http://rook-primus-g2:7400",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
            generation=2,
        )
        await s.commit()

    async with sessionmaker() as s:
        active = await store.get_active(s, "rook-primus")
        assert active is not None
        assert active.generation == 2

        old = await store.get(s, "rook-primus", 1)
        new = await store.get(s, "rook-primus", 2)
        assert old is not None and old.status == "draining"
        assert new is not None and new.status == "active"


@pytest.mark.asyncio
async def test_heartbeat_and_retire(sessionmaker) -> None:
    async with sessionmaker() as s:
        await _seed_persona(s)
        await store.register_agent(
            s,
            name="rook-primus",
            persona_name="rook",
            role="primus",
            endpoint="http://x:7400",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
        )
        await s.commit()

    async with sessionmaker() as s:
        await store.heartbeat(s, "rook-primus", 1)
        await s.commit()
        row = await store.get(s, "rook-primus", 1)
        assert row is not None and row.last_heartbeat is not None

    async with sessionmaker() as s:
        await store.mark_retired(s, "rook-primus", 1)
        await s.commit()
        assert await store.get_active(s, "rook-primus") is None
        row = await store.get(s, "rook-primus", 1)
        assert row is not None and row.status == "retired"
