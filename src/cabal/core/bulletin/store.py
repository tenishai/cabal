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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cabal.core.models import Agent, Persona


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
    kind: str = "member"
    federation_audience: str | None = None
    work: dict | None = None

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
            kind=row.kind,
            federation_audience=row.federation_audience,
            work=row.work_json,
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
    kind: str = "member",
    federation_audience: str | None = None,
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
        kind=kind,
        federation_audience=federation_audience,
    )
    session.add(row)
    await session.flush()
    return AgentRow.from_orm(row)


async def register_federated(
    session: AsyncSession,
    *,
    name: str,
    persona_name: str,
    endpoint: str,
    federation_audience: str,
    agent_card_json: dict,
    agent_card_kid: str,
    capabilities: dict | None = None,
    safety_tier: str = "approval-required",
    generation: int = 1,
) -> AgentRow:
    """Register a *federated* bulletin entry — a sovereign peer persona this
    cabal is a client of (cabal#45).

    `endpoint` is the sovereign service's HTTPS URL; `federation_audience` is its
    stable service id, which the caller mints into the dispatch token `aud` and
    which `_prepare` verifies against. `agent_card_json`/`agent_card_kid` hold the
    sovereign service's own signed AgentCard.
    """
    return await register_agent(
        session,
        name=name,
        persona_name=persona_name,
        role="singleton",
        endpoint=endpoint,
        capabilities=capabilities or {},
        safety_tier=safety_tier,
        agent_card_json=agent_card_json,
        agent_card_kid=agent_card_kid,
        generation=generation,
        status="active",
        kind="federated",
        federation_audience=federation_audience,
    )


SESSION_KIND = "session"
#: Every session row hangs off one synthetic persona so the `agents.persona_name`
#: FK holds without inventing a persona per session. It is deliberately NOT a
#: dispatchable persona — nothing in identity.toml declares it.
SESSION_PERSONA = "_session"


async def ensure_session_persona(session: AsyncSession) -> None:
    """Create the synthetic persona backing session rows, once, idempotently."""
    existing = await session.get(Persona, SESSION_PERSONA)
    if existing is None:
        session.add(
            Persona(
                name=SESSION_PERSONA,
                config_json={
                    "synthetic": True,
                    "reason": "backs kind='session' presence rows (cabal#142)",
                },
            )
        )
        await session.flush()


async def register_session(
    session: AsyncSession,
    *,
    name: str,
    work: dict | None = None,
    generation: int | None = None,
) -> AgentRow:
    """Register a live agent session as PRESENCE ONLY (cabal#142).

    A Claude Code session is not dispatchable: it runs no inbound listener and
    holds no signed AgentCard. The row exists so `cbl status` can answer "what
    is actually running and what is it doing" — the question the bulletin could
    not answer while it listed only the three personas.

    That non-dispatchability is structural, and the reason is NOT this row's
    empty ``capabilities`` map -- capability routing never reads the bulletin.
    `DispatchRouter.select_by_capabilities` matches against an IN-MEMORY
    ``_capabilities`` dict populated in exactly one place,
    `persona_factory.register_capabilities`, for personas declared in
    identity.toml. Registering a session never touches it, so a session name is
    not a key with an empty value -- it is not a key at all, and no
    `required_capabilities` can select it.

    The empty card and the ``session://`` endpoint (no transport dials that
    scheme) are the belt to that suspenders. Kept empty deliberately, so that if
    capability routing ever does learn to read the bulletin, a session still
    advertises nothing.
    """
    await ensure_session_persona(session)
    # NOT generation=1. A session name is deliberately STABLE across restarts
    # (CABAL_SESSION_NAME), so register -> end -> register is the ordinary daily
    # cycle, and `end` retires the row rather than deleting it. Hardcoding 1
    # collides with the retired row on the composite PK the second time a
    # session ever starts. `next_generation` scans all statuses; its own
    # docstring warns about exactly this and it sits forty lines up this file.
    # Two concurrent registers of the same name can both read MAX and both
    # insert; the loser gets a UNIQUE failure. DO NOT ADD A LOCK OR A RETRY --
    # `beat` is self-healing (`heartbeat || register`), so the loser's next
    # Stop 404s on heartbeat and re-registers. The window is one turn
    # (PR #148 review, nit 2).
    if generation is None:
        generation = await next_generation(session, name)
    row = await register_agent(
        session,
        name=name,
        persona_name=SESSION_PERSONA,
        role="session",
        endpoint=f"session://{name}",
        capabilities={},
        safety_tier="strict",
        agent_card_json={},
        agent_card_kid="",
        generation=generation,
        status="active",
        kind=SESSION_KIND,
    )
    if work is not None:
        await heartbeat(session, name, generation, work=work)
        return await get(session, name, generation) or row
    return row


async def get_active(session: AsyncSession, name: str) -> AgentRow | None:
    """Return the active generation for `name`, or None.

    Uses the partial index `idx_agents_name_active`.
    """
    result = await session.execute(
        select(Agent).where(Agent.name == name, Agent.status == "active")
    )
    row = result.scalar_one_or_none()
    return AgentRow.from_orm(row) if row else None


async def next_generation(session: AsyncSession, name: str) -> int:
    """Return the next free generation for `name`: MAX(generation) + 1.

    Scans ALL statuses (active/draining/retired), not just the active row, so
    the (name, generation) composite PK can never collide. Returns 1 when the
    name has no rows at all. Callers deriving the next generation from
    `get_active().generation + 1` collide after an `absorb` retires the active
    row without a successor — use this instead (cabal#45).
    """
    result = await session.execute(
        select(func.max(Agent.generation)).where(Agent.name == name)
    )
    current_max = result.scalar_one_or_none()
    return (current_max + 1) if current_max is not None else 1


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


async def heartbeat(
    session: AsyncSession,
    name: str,
    generation: int,
    *,
    work: dict | None = None,
) -> None:
    """Bump `last_heartbeat`, and for a session row also record what it is doing.

    `work` is written ONLY when a payload is supplied. Passing None leaves any
    existing value alone rather than clearing it: a heartbeat is a liveness
    signal, and a caller with nothing new to report should refresh liveness
    without destroying the last thing the session said it was doing. To clear
    it deliberately, pass an empty dict.

    This also protects against a persona heartbeat blanking a session's work
    line -- though only in the collision case, so do not read it as the main
    reason. `HeartbeatScheduler` pings only the names in its `_local_names`
    set, which sessions are never added to, so the two populations do not
    overlap unless an operator sets CABAL_SESSION_NAME to a persona's name.
    """
    values: dict = {"last_heartbeat": func.current_timestamp()}
    if work is not None:
        values["work_json"] = work
    await session.execute(
        update(Agent)
        .where(Agent.name == name, Agent.generation == generation)
        .values(**values)
    )
