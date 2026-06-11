"""Heartbeat scheduler tests."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.broker.heartbeat import HeartbeatScheduler
from cabal.core.bulletin import store as bulletin_store
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


async def _seed(sessionmaker, name: str = "echo") -> None:
    async with sessionmaker() as s:
        s.add(Persona(name=name, config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name=name,
            persona_name=name,
            role="builtin",
            endpoint=f"local://{name}",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
        )
        await s.commit()


@pytest.mark.asyncio
async def test_immediate_first_ping(sessionmaker) -> None:
    await _seed(sessionmaker)
    sched = HeartbeatScheduler(sessionmaker, interval_seconds=10.0)
    sched.watch(["echo"])
    await sched.start()
    # Give the initial ping a chance
    await asyncio.sleep(0.05)
    await sched.stop()

    async with sessionmaker() as s:
        row = await bulletin_store.get(s, "echo", 1)
    assert row is not None and row.last_heartbeat is not None


@pytest.mark.asyncio
async def test_skips_unwatched_agents(sessionmaker) -> None:
    await _seed(sessionmaker, "echo")
    await _seed(sessionmaker, "other")
    sched = HeartbeatScheduler(sessionmaker, interval_seconds=10.0)
    sched.watch(["echo"])
    await sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()

    async with sessionmaker() as s:
        echo_row = await bulletin_store.get(s, "echo", 1)
        other_row = await bulletin_store.get(s, "other", 1)
    assert echo_row.last_heartbeat is not None
    assert other_row.last_heartbeat is None


@pytest.mark.asyncio
async def test_stop_is_clean(sessionmaker) -> None:
    sched = HeartbeatScheduler(sessionmaker, interval_seconds=10.0)
    await sched.start()
    await sched.stop()
    # Re-stop should be a no-op
    await sched.stop()
