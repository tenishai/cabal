"""Schema tests against an in-memory SQLite DB.

These exercise the constraints that make the bulletin board work:
* coexisting generations on the same agent name (composite PK)
* the partial index on active rows (used by name lookup)
* composite FK from conversations into agents(name, generation)
* the self-referencing FK on cabal_escalated_from
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Agent, Base, Conversation, Persona


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


async def _make_persona(s: AsyncSession, name: str = "mason") -> Persona:
    p = Persona(name=name, config_json={"role": "primus"})
    s.add(p)
    await s.flush()
    return p


def _agent_row(name: str, generation: int, persona: str, status: str = "active") -> Agent:
    return Agent(
        name=name,
        generation=generation,
        persona_name=persona,
        role="primus",
        endpoint=f"http://{name}:7400",
        capabilities_json={"skills": []},
        safety_tier="strict",
        agent_card_json={"name": name, "generation": generation},
        agent_card_kid="key-001",
        status=status,
    )


@pytest.mark.asyncio
async def test_two_generations_coexist(sessionmaker) -> None:
    """The composite PK on (name, generation) lets old + new rows live together."""
    async with sessionmaker() as s:
        await _make_persona(s)
        s.add(_agent_row("mason-primus", 1, "mason", status="draining"))
        s.add(_agent_row("mason-primus", 2, "mason", status="active"))
        await s.commit()

        result = await s.execute(
            select(Agent).where(Agent.name == "mason-primus").order_by(Agent.generation)
        )
        rows = result.scalars().all()
        assert len(rows) == 2
        assert {r.generation for r in rows} == {1, 2}
        assert {r.status for r in rows} == {"draining", "active"}


@pytest.mark.asyncio
async def test_duplicate_generation_rejected(sessionmaker) -> None:
    """Two rows with the same (name, generation) violate the PK."""
    async with sessionmaker() as s:
        await _make_persona(s)
        s.add(_agent_row("mason-primus", 1, "mason"))
        await s.commit()

    async with sessionmaker() as s:
        s.add(_agent_row("mason-primus", 1, "mason"))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_conversation_fk_into_agents(sessionmaker) -> None:
    """conversations.(target_agent, target_generation) must FK into agents."""
    async with sessionmaker() as s:
        await _make_persona(s)
        s.add(_agent_row("mason-primus", 1, "mason"))
        await s.commit()

    # Valid: (mason-primus, 1) exists
    async with sessionmaker() as s:
        s.add(
            Conversation(
                task_id="t-1",
                target_agent="mason-primus",
                target_generation=1,
                state="working",
            )
        )
        await s.commit()

    # Invalid: (mason-primus, 99) doesn't exist
    async with sessionmaker() as s:
        s.add(
            Conversation(
                task_id="t-2",
                target_agent="mason-primus",
                target_generation=99,
                state="working",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_escalation_self_fk(sessionmaker) -> None:
    """cabal_escalated_from FKs back into conversations.task_id."""
    async with sessionmaker() as s:
        await _make_persona(s)
        s.add(_agent_row("mason-primus", 1, "mason"))
        await s.flush()
        s.add(
            Conversation(
                task_id="parent",
                target_agent="mason-primus",
                target_generation=1,
                state="completed",
            )
        )
        s.add(
            Conversation(
                task_id="child",
                target_agent="mason-primus",
                target_generation=1,
                state="working",
                cabal_escalated_from="parent",
            )
        )
        await s.commit()

        result = await s.execute(
            select(Conversation).where(Conversation.cabal_escalated_from == "parent")
        )
        assert result.scalar_one().task_id == "child"


@pytest.mark.asyncio
async def test_active_generation_lookup(sessionmaker) -> None:
    """The partial index on (name) WHERE status='active' is what the broker
    uses to resolve operator dispatches to the latest generation."""
    async with sessionmaker() as s:
        await _make_persona(s)
        s.add(_agent_row("mason-primus", 1, "mason", status="draining"))
        s.add(_agent_row("mason-primus", 2, "mason", status="active"))
        await s.commit()

        result = await s.execute(
            select(Agent).where(Agent.name == "mason-primus", Agent.status == "active")
        )
        active = result.scalar_one()
        assert active.generation == 2
