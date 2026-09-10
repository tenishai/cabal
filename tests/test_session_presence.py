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
async def test_a_session_carries_no_dispatchable_surface(sessionmaker) -> None:
    """The row advertises nothing: `session://` dials on no transport, and the
    card and capability map are empty.

    NOTE these are defence in depth, NOT the reason a session cannot be routed
    to -- see test_capability_routing_cannot_see_a_session for the mechanism
    that actually holds. Asserting only these would pin a property that does
    not cause the outcome.
    """
    async with sessionmaker() as s:
        row = await store.register_session(s, name="poe3")
        assert row.endpoint == "session://poe3"
        assert row.agent_card_json == {}
        assert row.capabilities == {}


@pytest.mark.asyncio
async def test_capability_routing_cannot_see_a_session(sessionmaker) -> None:
    """THE REAL MECHANISM, and it is stronger than the empty capability map.

    `select_by_capabilities` matches against the router's IN-MEMORY
    `_capabilities` dict, which is populated in exactly one place --
    `persona_factory` calling `register_capabilities` for identity.toml
    personas. Registering a session never touches it, so a session name is
    not a key holding an empty set; it is absent entirely.
    """
    from cabal.core.broker.router import DispatchRouter

    async with sessionmaker() as s:
        await store.register_session(s, name="poe3")

    # `_bookkeeper` / `_jwks_provider` are stored and never touched by the
    # capability registry, so stand-ins keep this a unit test of the routing
    # question rather than a wiring test of the whole broker.
    router = DispatchRouter(
        sessionmaker=sessionmaker, bookkeeper=object(), jwks_provider=object()
    )
    router.register_capabilities("archivist", ["search"])

    # The docstring claims the session is "not a key at all", which is stronger
    # than "a key holding an empty set". `capabilities_for` cannot tell those
    # apart -- it returns set() for both -- so the private dict is the only
    # assertion target that discriminates, and the claim is worth pinning
    # because it is what makes the guarantee independent of the empty map.
    assert "poe3" not in router._capabilities
    assert "archivist" in router._capabilities

    # And the outcome, which holds regardless of the mechanism.
    for required in (["search"], ["anything"], []):
        assert router.select_by_capabilities(required) != "poe3"


@pytest.mark.asyncio
async def test_register_end_register_is_the_ordinary_cycle_not_an_edge(
    sessionmaker,
) -> None:
    """THE CRITICAL FROM PR #148 REVIEW.

    A session name is deliberately STABLE across restarts (CABAL_SESSION_NAME),
    and `end` retires the row rather than deleting it. So the SECOND time a
    session ever starts, a hardcoded generation=1 collides with the retired
    generation=1 on the composite PK:

        IntegrityError: UNIQUE constraint failed: agents.name, agents.generation

    What made it Critical is the silence: heartbeat-after-end 404s, so the
    hook's `beat` falls back to `register`, which 500s, and both disappear into
    `>/dev/null 2>&1` behind `exit 0`. The session is simply never seen again
    and nothing says why.
    """
    async with sessionmaker() as s:
        first = await store.register_session(s, name="poe2")
        assert first.generation == 1
        await store.mark_retired(s, "poe2", first.generation)

        second = await store.register_session(s, name="poe2", work={"task": "day two"})
        assert second.generation == 2
        active = await store.get_active(s, "poe2")
        assert active is not None
        assert active.generation == 2
        assert active.work == {"task": "day two"}


@pytest.mark.asyncio
async def test_registering_over_a_draining_row_does_not_collide(sessionmaker) -> None:
    """`draining` is also invisible to get_active, so it takes the same path."""
    async with sessionmaker() as s:
        first = await store.register_session(s, name="poe2")
        await store.mark_draining(s, "poe2", first.generation)
        second = await store.register_session(s, name="poe2")
        assert second.generation == 2


@pytest.mark.asyncio
async def test_a_third_start_keeps_climbing(sessionmaker) -> None:
    """next_generation scans MAX over all statuses, so this holds past two."""
    async with sessionmaker() as s:
        for expected in (1, 2, 3):
            row = await store.register_session(s, name="poe2")
            assert row.generation == expected
            await store.mark_retired(s, "poe2", row.generation)


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
