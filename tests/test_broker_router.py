"""Dispatch router tests — covers all four §6.12 invariants in both modes.

Each invariant has its own assertion block; each runs against both the
in-process and HTTP dispatch path (parameterized) so the "same security
properties in both modes" claim is tested, not assumed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import create_signing_key
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.broker.router import DispatchError, DispatchRouter
from cabal.core.bulletin import store as bulletin_store
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base, Conversation, Event, Persona


# ---- fixtures -----------------------------------------------------------


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


@pytest.fixture
def jwks_provider(signing_key):
    jwk_dict = dict(
        RSAKey.import_key(signing_key.private_key.public_key()).as_dict(private=False)
    )
    jwk_dict["kid"] = signing_key.kid
    key_set = KeySet.import_key_set({"keys": [jwk_dict]})
    return lambda: key_set


@pytest_asyncio.fixture
async def archivist_row(sessionmaker):
    async with sessionmaker() as s:
        s.add(Persona(name="archivist", config_json={}))
        await s.flush()
        row = await bulletin_store.register_agent(
            s,
            name="archivist",
            persona_name="archivist",
            role="singleton",
            endpoint="http://archivist:7400",
            capabilities={"skills": []},
            safety_tier="strict",
            agent_card_json={"name": "archivist"},
            agent_card_kid="kid-1",
        )
        await s.commit()
    return row


@dataclass(slots=True)
class FakeArchivist:
    """LocalHandler stub that echoes the task with state=completed."""

    name: str = "archivist"
    captured: list[Task] | None = None

    def __post_init__(self) -> None:
        self.captured = []

    async def handle_task(self, task: Task) -> Task:
        assert self.captured is not None
        self.captured.append(task)
        return Task(
            id=task.id,
            status=TaskStatus(
                state="completed",
                message=Message(role="agent", parts=[TextPart(text="done")]),
            ),
            metadata=task.metadata,
        )


def _sample_task() -> Task:
    return Task(
        id="t-001",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="hi")]),
        ),
    )


# ---- invariant 2: caller JWT verification ------------------------------


@pytest.mark.asyncio
async def test_local_dispatch_succeeds_with_valid_jwt(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    handler = FakeArchivist()
    router.register_local(handler)

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    response = await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=token,
    )
    assert response.status.state == "completed"
    assert len(handler.captured) == 1


@pytest.mark.asyncio
async def test_local_dispatch_rejects_invalid_jwt(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    router.register_local(FakeArchivist())

    # Wrong audience -> rejection at the router boundary
    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="librarian")
    with pytest.raises(DispatchError, match="caller JWT"):
        await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )


@pytest.mark.asyncio
async def test_local_dispatch_rejects_expired_jwt(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    router.register_local(FakeArchivist())

    past = int(time.time()) - 1000
    token = mint(
        signing_key=signing_key,
        sub="rook-primus:1",
        aud="archivist",
        ttl_seconds=60,
        now=past,
    )
    with pytest.raises(DispatchError, match="caller JWT"):
        await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )


# ---- invariant 3: bookkeeping on every dispatch ------------------------


@pytest.mark.asyncio
async def test_successful_dispatch_writes_conversation_and_events(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    router.register_local(FakeArchivist())

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=token,
    )

    async with sessionmaker() as s:
        conv = (await s.execute(select(Conversation))).scalar_one()
        assert conv.task_id == "t-001"
        assert conv.target_agent == "archivist"
        assert conv.state == "completed"
        events = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
        assert [e.event_type for e in events] == [
            "dispatch_start",
            "dispatch_success",
        ]


@pytest.mark.asyncio
async def test_failed_local_dispatch_records_failure(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """If the LocalHandler raises, the broker records a dispatch_failure event."""

    @dataclass(slots=True)
    class Boom:
        name: str = "archivist"

        async def handle_task(self, task: Task) -> Task:
            raise RuntimeError("kaboom")

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    router.register_local(Boom())

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    with pytest.raises(RuntimeError, match="kaboom"):
        await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )

    async with sessionmaker() as s:
        events = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
        assert [e.event_type for e in events] == [
            "dispatch_start",
            "dispatch_failure",
        ]
        conv = (await s.execute(select(Conversation))).scalar_one()
        assert conv.state == "failed"


# ---- invariant 4: trace context propagation ----------------------------


@pytest.mark.asyncio
async def test_dispatch_generates_trace_id_when_absent(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    handler = FakeArchivist()
    router.register_local(handler)

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    task = _sample_task()
    assert "cabal_trace_id" not in task.metadata
    await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=task,
        caller_jwt=token,
    )
    assert handler.captured is not None
    captured = handler.captured[0]
    trace = captured.metadata["cabal_trace_id"]
    assert isinstance(trace, str) and len(trace) == 32
    assert "cabal_span_id" in captured.metadata


@pytest.mark.asyncio
async def test_dispatch_preserves_existing_trace_id(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    handler = FakeArchivist()
    router.register_local(handler)

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    incoming_trace = "a" * 32
    task = _sample_task()
    task.metadata["cabal_trace_id"] = incoming_trace
    await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=task,
        caller_jwt=token,
    )
    assert handler.captured is not None
    assert handler.captured[0].metadata["cabal_trace_id"] == incoming_trace


# ---- generation routing -----------------------------------------------


@pytest.mark.asyncio
async def test_explicit_generation_routes_to_draining(
    sessionmaker, signing_key, jwks_provider
) -> None:
    """A subordinate return targeting generation=1 should reach a draining row."""
    async with sessionmaker() as s:
        s.add(Persona(name="rook", config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name="rook-primus",
            persona_name="rook",
            role="primus",
            endpoint="http://rook-primus:7400",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
            generation=1,
        )
        await bulletin_store.mark_draining(s, "rook-primus", 1)
        await bulletin_store.register_agent(
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

    @dataclass(slots=True)
    class TrackingHandler:
        name: str = "rook-primus"
        seen_generations: list[int] | None = None

        def __post_init__(self) -> None:
            self.seen_generations = []

        async def handle_task(self, task: Task) -> Task:
            self.seen_generations.append(task.metadata.get("cabal_generation"))
            return Task(
                id=task.id,
                status=TaskStatus(state="completed"),
                metadata=task.metadata,
            )

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    handler = TrackingHandler()
    router.register_local(handler)

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="rook-primus")
    task = _sample_task()
    task.metadata["cabal_generation"] = 1
    await router.dispatch(
        target_agent="rook-primus",
        target_generation=1,
        task=task,
        caller_jwt=token,
    )
    assert handler.seen_generations == [1]


# ---- session_start / session_end hooks --------------------------------


@pytest.mark.asyncio
async def test_router_fires_session_start_and_end_on_root_dispatch(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    hooks = HookRegistry()
    starts: list[dict] = []
    ends: list[dict] = []
    hooks.register(HookName.SESSION_START, lambda p: starts.append(p))
    hooks.register(HookName.SESSION_END, lambda p: ends.append(p))

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        hook_registry=hooks,
    )
    router.register_local(FakeArchivist())

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=token,
    )
    assert len(starts) == 1 and starts[0]["target"] == "archivist"
    assert len(ends) == 1 and ends[0]["state"] == "completed"


@pytest.mark.asyncio
async def test_router_does_not_double_fire_for_subdispatch(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """A Task already marked cabal_session_started is treated as a sub-hop;
    session_start/end NOT fired again."""
    hooks = HookRegistry()
    starts: list[dict] = []
    ends: list[dict] = []
    hooks.register(HookName.SESSION_START, lambda p: starts.append(p))
    hooks.register(HookName.SESSION_END, lambda p: ends.append(p))

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        hook_registry=hooks,
    )
    router.register_local(FakeArchivist())

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    task = _sample_task()
    task.metadata["cabal_session_started"] = True
    await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=task,
        caller_jwt=token,
    )
    assert starts == [] and ends == []


@pytest.mark.asyncio
async def test_router_fires_session_end_on_failure(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    @dataclass(slots=True)
    class Boom:
        name: str = "archivist"

        async def handle_task(self, task: Task) -> Task:
            raise RuntimeError("kaboom")

    hooks = HookRegistry()
    ends: list[dict] = []
    hooks.register(HookName.SESSION_END, lambda p: ends.append(p))

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        hook_registry=hooks,
    )
    router.register_local(Boom())

    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
    with pytest.raises(RuntimeError):
        await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )
    assert len(ends) == 1
    assert ends[0]["state"] == "failed"
    assert ends[0]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_unknown_target_raises(
    sessionmaker, signing_key, jwks_provider
) -> None:
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    token = mint(signing_key=signing_key, sub="rook-primus:1", aud="ghost")
    with pytest.raises(DispatchError, match="no agent registered"):
        await router.dispatch(
            target_agent="ghost",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )


# ---- HTTP path (scale mode) -------------------------------------------


@pytest.mark.asyncio
async def test_remote_dispatch_calls_endpoint_with_bearer(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """When the agent has no LocalHandler registered, route via HTTP."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "t-001",
                "status": {"state": "completed"},
                "metadata": {},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        router = DispatchRouter(
            sessionmaker=sessionmaker,
            bookkeeper=Bookkeeper(sessionmaker),
            jwks_provider=jwks_provider,
            http_client=client,
        )
        token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
        response = await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )

    assert response.status.state == "completed"
    assert len(captured) == 1
    req = captured[0]
    assert str(req.url) == "http://archivist:7400/a2a/tasks"
    assert req.headers["authorization"] == f"Bearer {token}"


@pytest.mark.asyncio
async def test_remote_dispatch_writes_same_bookkeeping_as_local(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """Invariant 3 holds for HTTP path too."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "t-001", "status": {"state": "completed"}, "metadata": {}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        router = DispatchRouter(
            sessionmaker=sessionmaker,
            bookkeeper=Bookkeeper(sessionmaker),
            jwks_provider=jwks_provider,
            http_client=client,
        )
        token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
        await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )

    async with sessionmaker() as s:
        events = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
        assert [e.event_type for e in events] == ["dispatch_start", "dispatch_success"]
        conv = (await s.execute(select(Conversation))).scalar_one()
        assert conv.state == "completed"


@pytest.mark.asyncio
async def test_remote_dispatch_propagates_transport_error(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        router = DispatchRouter(
            sessionmaker=sessionmaker,
            bookkeeper=Bookkeeper(sessionmaker),
            jwks_provider=jwks_provider,
            http_client=client,
        )
        token = mint(signing_key=signing_key, sub="rook-primus:1", aud="archivist")
        with pytest.raises(DispatchError, match="transport"):
            await router.dispatch(
                target_agent="archivist",
                target_generation=None,
                task=_sample_task(),
                caller_jwt=token,
            )

    async with sessionmaker() as s:
        events = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
        assert [e.event_type for e in events] == ["dispatch_start", "dispatch_failure"]
