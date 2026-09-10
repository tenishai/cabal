"""Session presence rows on the bulletin board (cabal#142).

The bulletin listed three personas answering a local model while the actual
fleet -- Claude Code sessions across repos -- was invisible to the operator.
These pin the presence row: it shows up, it carries what the session is doing,
and it can never be dispatched to.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
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


@pytest.mark.asyncio
async def test_a_session_registers_and_appears_as_active(sessionmaker) -> None:
    async with sessionmaker() as s:
        row = await store.register_session(s, name="poe2")
        assert row.kind == "session"
        assert row.status == "active"
        assert (await store.get_active(s, "poe2")) is not None


@pytest.mark.asyncio
async def test_the_synthetic_persona_is_created_once_not_per_session(
    sessionmaker,
) -> None:
    """Two sessions must not collide on the FK-backing persona row."""
    async with sessionmaker() as s:
        await store.register_session(s, name="poe1")
        await store.register_session(s, name="poe2")
        await s.commit()
    async with sessionmaker() as s:
        personas = (await s.execute(select(Persona))).scalars().all()
        assert [p.name for p in personas] == [store.SESSION_PERSONA]


@pytest.mark.asyncio
async def test_a_session_is_structurally_undispatchable(sessionmaker) -> None:
    """Non-dispatchability is encoded, not documented.

    An empty capability map cannot satisfy any `select_by_capabilities`
    requirement, and a `session://` endpoint is not dialable by any transport.
    """
    async with sessionmaker() as s:
        row = await store.register_session(s, name="poe3")
        assert row.capabilities == {}
        assert row.endpoint == "session://poe3"
        assert row.agent_card_json == {}


@pytest.mark.asyncio
async def test_heartbeat_records_what_the_session_is_doing(sessionmaker) -> None:
    async with sessionmaker() as s:
        await store.register_session(s, name="poe2")
        await store.heartbeat(
            s,
            "poe2",
            1,
            work={"repo": "liminal", "branch": "fix/73", "task": "hardening bill"},
        )
        row = await store.get_active(s, "poe2")
        assert row is not None
        assert row.work == {
            "repo": "liminal",
            "branch": "fix/73",
            "task": "hardening bill",
        }
        assert row.last_heartbeat is not None


@pytest.mark.asyncio
async def test_a_heartbeat_without_work_does_not_erase_the_work_line(
    sessionmaker,
) -> None:
    """THE REGRESSION THIS EXISTS FOR: the persona heartbeat scheduler calls
    heartbeat() with no work payload. If that wrote NULL, every scheduler tick
    would blank the work line of every session and the operator view would be
    empty exactly when it is being kept warm."""
    async with sessionmaker() as s:
        await store.register_session(s, name="poe2", work={"task": "reviewing #91"})
        await store.heartbeat(s, "poe2", 1)
        row = await store.get_active(s, "poe2")
        assert row is not None
        assert row.work == {"task": "reviewing #91"}


@pytest.mark.asyncio
async def test_work_can_be_cleared_deliberately_with_an_empty_dict(
    sessionmaker,
) -> None:
    async with sessionmaker() as s:
        await store.register_session(s, name="poe2", work={"task": "x"})
        await store.heartbeat(s, "poe2", 1, work={})
        row = await store.get_active(s, "poe2")
        assert row is not None
        assert row.work == {}


@pytest.mark.asyncio
async def test_register_with_work_returns_the_work_on_the_first_row(
    sessionmaker,
) -> None:
    """Registration is the first heartbeat -- an operator running `cbl status`
    immediately after a session starts should not see a blank line."""
    async with sessionmaker() as s:
        row = await store.register_session(
            s, name="poe2", work={"repo": "cabal", "task": "hitting #142"}
        )
        assert row.work == {"repo": "cabal", "task": "hitting #142"}


@pytest.mark.asyncio
async def test_a_persona_row_carries_no_work(sessionmaker) -> None:
    """work_json is additive: existing rows and persona rows read as None."""
    async with sessionmaker() as s:
        s.add(Persona(name="mason", config_json={}))
        await s.flush()
        row = await store.register_agent(
            s,
            name="mason-primus",
            persona_name="mason",
            role="primus",
            endpoint="local://mason-primus",
            capabilities={"build": True},
            safety_tier="strict",
            agent_card_json={"name": "mason-primus"},
            agent_card_kid="k1",
        )
        assert row.work is None
        assert row.kind == "member"
