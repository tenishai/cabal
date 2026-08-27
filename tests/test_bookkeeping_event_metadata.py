"""Bookkeeper._publish must carry task metadata so SSE consumers can scope
their streams.

kobo's admin event stream filters frames by `payload.metadata` (e.g. a
`thread` key) to show only the events that belong to the room a viewer has
open. Bookkeeper.on_dispatch_start builds its published payload from
target/generation/caller/trace_id only -- the task's own `metadata` dict
never made it into the event -- so kobo's filter can never fire and every
viewer sees every task's events.

This pins that a task with metadata={"thread": "t-1"} produces a
dispatch_start event on the EventBus whose payload includes that same
metadata, verbatim.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.bulletin.store import AgentRow, register_agent
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base, Persona
from cabal.core.observability.event_bus import EventBus


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


def _task_with_metadata(metadata: dict) -> Task:
    return Task(
        id="t-001",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="hi")]),
        ),
        metadata=metadata,
    )


async def _persist_target(sessionmaker) -> AgentRow:
    # Bookkeeper.on_dispatch_start FKs its conversations row against the
    # agents table, so the target must genuinely exist, not just be a
    # dataclass snapshot (the same requirement test_admin_route.py notes).
    async with sessionmaker() as session:
        # agents.persona_name FKs personas.name, so the persona comes first
        # (same seeding order app.py's own bootstrap uses).
        session.add(Persona(name="archivist", config_json={"builtin": True}))
        await session.flush()
        row = await register_agent(
            session,
            name="archivist",
            persona_name="archivist",
            role="singleton",
            endpoint="http://archivist:7400",
            capabilities={"skills": []},
            safety_tier="strict",
            agent_card_json={"name": "archivist"},
            agent_card_kid="kid-1",
        )
        await session.commit()
    return row


@pytest.mark.asyncio
async def test_dispatch_start_event_carries_task_metadata(sessionmaker) -> None:
    bus = EventBus()
    queue = bus.subscribe()
    bookkeeper = Bookkeeper(sessionmaker, event_bus=bus)

    target = await _persist_target(sessionmaker)
    task = _task_with_metadata({"thread": "t-1"})
    await bookkeeper.on_dispatch_start(task, target, caller_agent="mason-primus")

    event = queue.get_nowait()
    assert event["event_type"] == "dispatch_start"
    assert event["payload"]["metadata"] == {"thread": "t-1"}
