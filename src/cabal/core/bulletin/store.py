"""Bulletin-board CRUD.

Backed by the `agents` table; rows are addressable by (name, generation).
The active-generation lookup uses the partial index from migration 0001.

Methods here are the only sanctioned interface between the dispatch
router and the `agents` table — keeping the SQL behind a thin facade so
the broker tests can substitute an in-memory fake later if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cabal.core.models import Agent


@dataclass(slots=True, frozen=True)
class AgentRow:
    """Read-only snapshot of an agents row."""

    name: str
    generation: int
    persona_name: str
    role: str
    endpoint: str
    capabilities: dict
    safety_tier: str
    agent_card_json: dict
    agent_card_kid: str
    status: str
    drain_started_at: datetime | None
    last_heartbeat: datetime | None

    @classmethod
    def from_orm(cls, row: Agent) -> AgentRow:
        return cls(
            name=row.name,
            generation=row.generation,
            persona_name=row.persona_name,
            role=row.role,
            endpoint=row.endpoint,
            capabilities=row.capabilities_json,
            safety_tier=row.safety_tier,
            agent_card_json=row.agent_card_json,
            agent_card_kid=row.agent_card_kid,
            status=row.status,
            drain_started_at=row.drain_started_at,
            last_heartbeat=row.last_heartbeat,
        )


async def register_agent(
    session: AsyncSession,
    *,
    name: str,
    persona_name: str,
    role: str,
    endpoint: str,
    capabilities: dict,
    safety_tier: str,
    agent_card_json: dict,
    agent_card_kid: str,
    generation: int = 1,
    status: str = "active",
) -> AgentRow:
    row = Agent(
        name=name,
        generation=generation,
        persona_name=persona_name,
        role=role,
        endpoint=endpoint,
        capabilities_json=capabilities,
        safety_tier=safety_tier,
        agent_card_json=agent_card_json,
        agent_card_kid=agent_card_kid,
        status=status,
    )
    session.add(row)
    await session.flush()
    return AgentRow.from_orm(row)


async def get_active(session: AsyncSession, name: str) -> AgentRow | None:
    """Return the active generation for `name`, or None.

    Uses the partial index `idx_agents_name_active`.
    """
    result = await session.execute(
        select(Agent).where(Agent.name == name, Agent.status == "active")
    )
    row = result.scalar_one_or_none()
    return AgentRow.from_orm(row) if row else None


async def get(
    session: AsyncSession, name: str, generation: int
) -> AgentRow | None:
    """Fetch an exact (name, generation) row regardless of status.

    Used by the broker to route subordinate returns back to the same
    generation that dispatched them, even when that generation is in
    `draining` status.
    """
    result = await session.execute(
        select(Agent).where(Agent.name == name, Agent.generation == generation)
    )
    row = result.scalar_one_or_none()
    return AgentRow.from_orm(row) if row else None


async def list_all(session: AsyncSession) -> list[AgentRow]:
    result = await session.execute(select(Agent).order_by(Agent.name, Agent.generation))
    return [AgentRow.from_orm(r) for r in result.scalars().all()]


async def mark_draining(session: AsyncSession, name: str, generation: int) -> None:
    await session.execute(
        update(Agent)
        .where(Agent.name == name, Agent.generation == generation)
        .values(status="draining")
    )


async def mark_retired(session: AsyncSession, name: str, generation: int) -> None:
    await session.execute(
        update(Agent)
        .where(Agent.name == name, Agent.generation == generation)
        .values(status="retired")
    )


async def heartbeat(session: AsyncSession, name: str, generation: int) -> None:
    from sqlalchemy import func

    await session.execute(
        update(Agent)
        .where(Agent.name == name, Agent.generation == generation)
        .values(last_heartbeat=func.current_timestamp())
    )
