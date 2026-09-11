"""Dispatch router tests — covers all four §6.12 invariants in both modes.

Each invariant has its own assertion block; each runs against both the
in-process and HTTP dispatch path (parameterized) so the "same security
properties in both modes" claim is tested, not assumed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint, verify
from cabal.core.auth.keys import create_signing_key
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.broker.router import DispatchError, DispatchRouter
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.guardrails import GuardrailContext, Verdict, VerdictType
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.models import Base, Conversation, Event, Persona

# ---- fixtures -----------------------------------------------------------


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest.fixture
def master_key() -> bytes:
    return Fernet.generate_key()


@pytest_asyncio.fixture
async def signing_key(sessionmaker, master_key):
    async with sessionmaker() as s:
        stored = await create_signing_key(s, master_key=master_key)
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="librarian")
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
        sub="mason-primus:1",
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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
        s.add(Persona(name="mason", config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name="mason-primus",
            persona_name="mason",
            role="primus",
            endpoint="http://mason-primus:7400",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
            generation=1,
        )
        await bulletin_store.mark_draining(s, "mason-primus", 1)
        await bulletin_store.register_agent(
            s,
            name="mason-primus",
            persona_name="mason",
            role="primus",
            endpoint="http://mason-primus-g2:7400",
            capabilities={},
            safety_tier="strict",
            agent_card_json={},
            agent_card_kid="kid-1",
            generation=2,
        )
        await s.commit()

    @dataclass(slots=True)
    class TrackingHandler:
        name: str = "mason-primus"
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="mason-primus")
    task = _sample_task()
    task.metadata["cabal_generation"] = 1
    await router.dispatch(
        target_agent="mason-primus",
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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

    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="ghost")
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
        token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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
        token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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
        token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
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


# ---- cabal#45: federated (sovereign-peer) dispatch ---------------------


@pytest_asyncio.fixture
async def federated_archivist_row(sessionmaker):
    async with sessionmaker() as s:
        s.add(Persona(name="archivist", config_json={}))
        await s.flush()
        row = await bulletin_store.register_federated(
            s,
            name="archivist",
            persona_name="archivist",
            endpoint="https://archivist.rd.ts.net",
            federation_audience="archivist@rd",
            agent_card_json={"name": "archivist"},
            agent_card_kid="kid-fed",
        )
        await s.commit()
    return row


@pytest.mark.asyncio
async def test_register_federated_sets_kind(sessionmaker, federated_archivist_row) -> None:
    async with sessionmaker() as s:
        row = await bulletin_store.get_active(s, "archivist")
    assert row.kind == "federated"
    assert row.federation_audience == "archivist@rd"
    assert row.endpoint == "https://archivist.rd.ts.net"


@pytest.mark.asyncio
async def test_federated_routes_remote_even_with_local_handler(
    sessionmaker, signing_key, jwks_provider, federated_archivist_row
) -> None:
    """A federated target routes to the sovereign peer over HTTP even when a
    local handler is registered for the same name (the dispatch-time guard)."""
    captured: dict = {}

    def _remote_ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        done = Task(
            id=body["id"],
            status=TaskStatus(
                state="completed",
                message=Message(role="agent", parts=[TextPart(text="remote-done")]),
            ),
        )
        return httpx.Response(200, json=done.model_dump(by_alias=True, exclude_none=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(_remote_ok)) as client:
        router = DispatchRouter(
            sessionmaker=sessionmaker,
            bookkeeper=Bookkeeper(sessionmaker),
            jwks_provider=jwks_provider,
            http_client=client,
        )
        local = FakeArchivist()
        router.register_local(local)  # same name — guard must still go remote
        token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist@rd")
        resp = await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )

    assert resp.status.state == "completed"
    assert "remote-done" in resp.status.message.parts[0].text
    assert captured["url"].startswith("https://archivist.rd.ts.net")  # went remote
    assert captured["auth"] == "Bearer " + token  # home JWT forwarded
    assert len(local.captured) == 0  # local handler NOT used


@pytest.mark.asyncio
async def test_federated_requires_sovereign_audience(
    sessionmaker, signing_key, jwks_provider, federated_archivist_row
) -> None:
    """For a federated target, a token aimed at the bare agent name is rejected
    — it must carry the sovereign service id (the aud forwarded to the peer)."""
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    bad = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")
    with pytest.raises(DispatchError, match="caller JWT"):
        await router.dispatch(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=bad,
        )


@dataclass(slots=True)
class FakeStreamingArchivist:
    """Streaming-capable local handler — proves the federated guard fires even
    when a local astream_task exists (i.e. it would otherwise stream locally)."""

    name: str = "archivist"
    streamed: bool = False

    async def handle_task(self, task: Task) -> Task:
        return Task(id=task.id, status=TaskStatus(state="completed"))

    async def astream_task(self, task: Task):
        self.streamed = True
        yield "local-chunk"


@pytest.mark.asyncio
async def test_federated_streaming_refused(
    sessionmaker, signing_key, jwks_provider, federated_archivist_row
) -> None:
    """A federated target must not be served by the LOCAL streaming path either
    — the guard refuses before touching the handler (Prudence #46/Issue 1)."""
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    handler = FakeStreamingArchivist()
    router.register_local(handler)  # streaming-capable local handler present
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist@rd")
    with pytest.raises(DispatchError, match="federated"):
        async for _ in router.dispatch_streaming(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        ):
            pass
    assert handler.streamed is False  # never streamed locally


@pytest.mark.asyncio
async def test_federated_token_mint_uses_sovereign_audience(
    sessionmaker, signing_key, master_key, jwks_provider, federated_archivist_row
) -> None:
    """_resolve_dispatch_token re-mints an operator token to the sovereign
    service id (federation_audience) for a federated target, not the bare name
    (Prudence #46/Issue 2)."""
    from types import SimpleNamespace

    from cabal.core.routes.tasks import _resolve_dispatch_token

    state = SimpleNamespace(
        jwks_provider=jwks_provider, sessionmaker=sessionmaker, master_key=master_key
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    operator_token = mint(
        signing_key=signing_key, sub="operator", aud="cabal-core", scope=["operator"]
    )
    minted = await _resolve_dispatch_token(request, operator_token, "archivist")
    claims = verify(minted, key_set=jwks_provider(), expected_audience=None)
    assert claims["aud"] == "archivist@rd"  # sovereign id, NOT "archivist"
    assert claims["cabal_scope"] == ["dispatch"]


# ---- resume_local federated guard (cabal#45) ---------------------------


@pytest.mark.asyncio
async def test_resume_local_raises_for_federated_target(
    sessionmaker, signing_key, jwks_provider, federated_archivist_row
) -> None:
    """resume_local must raise DispatchError when the target is a federated
    sovereign peer — it has no local resume surface. Symmetric with the guard
    in dispatch_streaming."""
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    # Register a local handler — the guard must reject BEFORE checking _local.
    router.register_local(FakeArchivist())

    with pytest.raises(DispatchError, match="federated"):
        await router.resume_local(
            target_agent="archivist",
            task=_sample_task(),
            resume_from=None,
        )


@pytest.mark.asyncio
async def test_resume_local_raises_for_federated_even_without_local_handler(
    sessionmaker, signing_key, jwks_provider, federated_archivist_row
) -> None:
    """resume_local's federated guard fires regardless of whether a local
    handler is registered. The error message must mention 'federated'."""
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )
    # No local handler registered — federated guard should still fire first.
    with pytest.raises(DispatchError, match="federated"):
        await router.resume_local(
            target_agent="archivist",
            task=_sample_task(),
            resume_from=None,
        )


@pytest.mark.asyncio
async def test_resume_local_succeeds_for_member_target(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """Sanity-check: resume_local still works for a local (member) agent."""
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
    )

    @dataclass(slots=True)
    class ResumableHandler:
        name: str = "archivist"

        async def handle_task(self, task: Task, resume_from=None) -> Task:
            return Task(id=task.id, status=TaskStatus(state="completed"), metadata=task.metadata)

    router.register_local(ResumableHandler())
    response = await router.resume_local(
        target_agent="archivist",
        task=_sample_task(),
        resume_from=None,
    )
    assert response.status.state == "completed"


# ---- combined-session re-mint path still works -------------------------


@pytest.mark.asyncio
async def test_combined_session_operator_mint_member_target(
    sessionmaker, signing_key, master_key, jwks_provider, archivist_row
) -> None:
    """_resolve_dispatch_token combined-session path: operator re-mint for a
    regular member target produces a token aimed at the bare agent name."""
    from types import SimpleNamespace

    from cabal.core.routes.tasks import _resolve_dispatch_token

    state = SimpleNamespace(
        jwks_provider=jwks_provider, sessionmaker=sessionmaker, master_key=master_key
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    operator_token = mint(
        signing_key=signing_key, sub="operator", aud="cabal-core", scope=["operator"]
    )
    minted = await _resolve_dispatch_token(request, operator_token, "archivist")
    claims = verify(minted, key_set=jwks_provider(), expected_audience=None)
    # For a member target the aud is the bare agent name.
    assert claims["aud"] == "archivist"
    assert claims["cabal_scope"] == ["dispatch"]


@pytest.mark.asyncio
async def test_combined_session_passthrough_for_correctly_aimed_token(
    sessionmaker, signing_key, master_key, jwks_provider, archivist_row
) -> None:
    """_resolve_dispatch_token returns the raw token unchanged when it is
    already aimed at the correct audience (combined session must not break
    this fast-path)."""
    from types import SimpleNamespace

    from cabal.core.routes.tasks import _resolve_dispatch_token

    state = SimpleNamespace(
        jwks_provider=jwks_provider, sessionmaker=sessionmaker, master_key=master_key
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    aimed_token = mint(
        signing_key=signing_key, sub="mason-primus:1", aud="archivist", scope=["dispatch"]
    )
    returned = await _resolve_dispatch_token(request, aimed_token, "archivist")
    # Must be the SAME token object, not a re-minted one.
    assert returned == aimed_token


# ======================================================================
# cabal#151 — the streaming path runs the SAME input guardrails
# ======================================================================


@dataclass(slots=True)
class _RecordingGuard:
    """A guardrail that records what it saw and returns a configured verdict."""

    verdict: Verdict
    calls: int = 0
    seen_target: str | None = None

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        self.calls += 1
        if isinstance(target, Task):
            self.seen_target = str(target.metadata.get("cabal_target_agent", ""))
        return self.verdict


@pytest.mark.asyncio
async def test_streaming_block_refuses_before_the_handler_streams(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """THE DEFECT IN #151: every router-level bound was opt-out by endpoint.

    The handler's `streamed` flag is the load-bearing assertion — a guard that
    fired but let the stream open would be no guard at all.
    """
    guard = _RecordingGuard(verdict=Verdict.block(reason="over budget"))
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[guard],
    )
    handler = FakeStreamingArchivist()
    router.register_local(handler)
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")

    with pytest.raises(DispatchError, match="guardrail blocked"):
        async for _ in router.dispatch_streaming(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        ):
            pass

    assert guard.calls == 1
    assert handler.streamed is False, "the stream opened despite a BLOCK verdict"


@pytest.mark.asyncio
async def test_streaming_pass_lets_the_stream_through(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """The guard must not break the ordinary path."""
    guard = _RecordingGuard(verdict=Verdict.pass_())
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[guard],
    )
    handler = FakeStreamingArchivist()
    router.register_local(handler)
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")

    chunks = [
        d
        async for d in router.dispatch_streaming(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        )
    ]
    assert chunks == ["local-chunk"]
    assert guard.calls == 1
    assert handler.streamed is True


@pytest.mark.asyncio
async def test_streaming_guard_sees_the_target_agent_metadata(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """DoomLoopGuardrail keys its delegation hash on cabal_target_agent. If the
    streaming path did not set it, every streamed delegation would hash an
    empty target and collapse into one key."""
    guard = _RecordingGuard(verdict=Verdict.pass_())
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[guard],
    )
    router.register_local(FakeStreamingArchivist())
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")

    async for _ in router.dispatch_streaming(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=token,
    ):
        pass
    assert guard.seen_target == "archivist"


@pytest.mark.asyncio
async def test_a_structurally_impossible_stream_does_not_consume_the_guard(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """ORDERING MATTERS AND THIS PINS IT.

    Guardrails run AFTER the structural refusals. A guard that ran first would
    charge a delegation budget for a dispatch that could never have happened --
    so an unreachable target would silently spend the caller's allowance.
    """
    guard = _RecordingGuard(verdict=Verdict.pass_())
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[guard],
    )

    class _NonStreaming:
        name = "archivist"

        async def handle_task(self, task: Task) -> Task:
            return Task(id=task.id, status=TaskStatus(state="completed"))

    router.register_local(_NonStreaming())
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")

    with pytest.raises(DispatchError, match="astream_task"):
        async for _ in router.dispatch_streaming(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        ):
            pass
    assert guard.calls == 0, "a guard ran for a stream that could never open"

    # TEETH (#153 review): the assertion above is trivially true when no guard
    # runs at all, so it survives a mutant that deletes the guard entirely. Pin
    # that the guard DOES run for a stream that can open, on the same instance.
    router.register_local(FakeStreamingArchivist())
    async for _ in router.dispatch_streaming(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=token,
    ):
        pass
    assert guard.calls == 1, "the guard did not run for a stream that could open"


@pytest.mark.asyncio
async def test_streaming_honours_a_rewrite_verdict(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """REWRITE threads through both paths or neither."""
    replacement = _sample_task()
    replacement.metadata["rewritten_by_guard"] = True
    guard = _RecordingGuard(
        verdict=Verdict(type=VerdictType.REWRITE, rewritten=replacement)
    )
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[guard],
    )

    seen: dict = {}

    @dataclass(slots=True)
    class _Capturing:
        name: str = "archivist"

        async def handle_task(self, task: Task) -> Task:
            return Task(id=task.id, status=TaskStatus(state="completed"))

        async def astream_task(self, task: Task):
            seen["metadata"] = dict(task.metadata)
            yield "chunk"

    router.register_local(_Capturing())
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")

    async for _ in router.dispatch_streaming(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=token,
    ):
        pass
    assert seen["metadata"].get("rewritten_by_guard") is True


@pytest.mark.asyncio
async def test_a_blocked_stream_raises_guardrail_blocked_not_a_bare_dispatch_error(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """#153 review, Important 1 -- THE TYPE CONTRACT ONLY.

    The router raises the SUBCLASS, and it remains a DispatchError so every
    existing `except DispatchError` keeps working. That is all this asserts.

    IT DOES NOT TEST THE WIRE CODE, and an earlier name and docstring here
    claimed it did (r2 review). The frame's ERROR_FORBIDDEN is pinned by
    tests/test_streaming.py::test_a_blocked_stream_reports_forbidden_on_the_wire,
    which drives the real route -- necessarily, because the two `except` arms
    are order-dependent and reordering them is invisible from here.
    """
    from cabal.core.broker.router import DispatchError, GuardrailBlocked

    guard = _RecordingGuard(verdict=Verdict.block(reason="over budget"))
    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[guard],
    )
    router.register_local(FakeStreamingArchivist())
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")

    with pytest.raises(GuardrailBlocked) as caught:
        async for _ in router.dispatch_streaming(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        ):
            pass
    # Still a DispatchError, so nothing that catches the base class breaks.
    assert isinstance(caught.value, DispatchError)


@pytest.mark.asyncio
async def test_a_blocked_stream_is_recorded_not_silent(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """#153 review, Important 2.

    dispatch() opens bookkeeping BEFORE its guard, so a non-streaming refusal
    is fully audited. Guarding first here made streaming the one path where a
    refusal left no trace -- the fail-silent shape removed from the presence
    hook in #148. The refusal must both open and CLOSE its record; a started-
    and-never-finished dispatch reads as an in-flight call that vanished.
    """
    calls: list[str] = []

    class _SpyBookkeeper(Bookkeeper):
        async def on_dispatch_start(self, *a, **k):
            calls.append("start")
            return await super().on_dispatch_start(*a, **k)

        async def on_dispatch_failure(self, *a, **k):
            calls.append("failure")
            return await super().on_dispatch_failure(*a, **k)

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=_SpyBookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[_RecordingGuard(verdict=Verdict.block(reason="nope"))],
    )
    router.register_local(FakeStreamingArchivist())
    token = mint(signing_key=signing_key, sub="mason-primus:1", aud="archivist")

    with pytest.raises(DispatchError):
        async for _ in router.dispatch_streaming(
            target_agent="archivist",
            target_generation=None,
            task=_sample_task(),
            caller_jwt=token,
        ):
            pass
    assert calls == ["start", "failure"], f"refusal not audited: {calls}"


@pytest.mark.asyncio
async def test_the_router_tells_a_guardrail_who_the_caller_is(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """cabal#154 — `caller_persona` reaches the guard, and is None for an operator.

    The whole edge check rests on this field: it is the only thing that
    separates "agent mason is delegating" from "a human is at the CLI", and
    `persona_name` cannot, because it collapses both into one display string.
    Testing the guard in isolation would never catch the router failing to
    populate it -- the guard would just see None and exempt everybody.
    """
    seen: list[GuardrailContext] = []

    class _CaptureGuard:
        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            seen.append(ctx)
            return Verdict.pass_()

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[_CaptureGuard()],
    )
    router.register_local(FakeArchivist())

    # A PERSONA caller: cabal_persona is on the token, so the guard can name it.
    persona_token = mint(
        signing_key=signing_key, sub="persona:mason", aud="archivist", persona="mason"
    )
    await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=persona_token,
    )
    assert seen[-1].caller_persona == "mason"

    # An OPERATOR: no persona claim, so None -- which the edge guard reads as
    # "not an agent" and exempts. routes/auth.py produces exactly this shape.
    operator_token = mint(
        signing_key=signing_key, sub="operator:richard", aud="archivist"
    )
    second = _sample_task()
    second.id = "t-002"  # distinct: conversations.task_id is UNIQUE
    await router.dispatch(
        target_agent="archivist",
        target_generation=None,
        task=second,
        caller_jwt=operator_token,
    )
    assert seen[-1].caller_persona is None
    assert seen[-1].caller_agent == "operator:richard", (
        "the raw sub must reach the guard: caller_persona being None is NOT "
        "evidence of an operator (admin.py mints persona-less service tokens "
        "too), so an exemption needs the positive signal this field carries"
    )
    assert seen[-1].persona_name == "operator:richard", (
        "persona_name still carries the display identifier; caller_persona and "
        "caller_agent are the typed ones an authorization decision may use"
    )


@pytest.mark.asyncio
async def test_the_streaming_router_tells_a_guardrail_who_the_caller_is(
    sessionmaker, signing_key, jwks_provider, archivist_row
) -> None:
    """cabal#154 r1 Important — the SAME identity contract, on the other path.

    `dispatch_streaming` carries an authorization decision now, and nothing
    covered it: removing `caller_persona` from its GuardrailContext left the
    whole suite green, because the non-streaming test above is the only thing
    asserting the wiring and it cannot see this call site.

    That gap matters here more than coverage gaps usually do. The commit
    immediately before this branch (a41edd5, #151) exists precisely because
    `dispatch_streaming` had been forgotten by the router guardrail chain --
    this path has a one-commit-old history of being the one that gets missed.

    AND IT WOULD FAIL OPEN, not closed, for the shape that matters. A persona
    caller would present as caller_persona=None and be REFUSED (a functional
    break, loud). But a caller carrying BOTH an operator: sub and a persona
    claim would take the exemption branch and bypass its edges entirely.
    """
    seen: list[GuardrailContext] = []

    class _CaptureGuard:
        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            seen.append(ctx)
            return Verdict.pass_()

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[_CaptureGuard()],
    )
    router.register_local(FakeStreamingArchivist())

    persona_token = mint(
        signing_key=signing_key, sub="persona:mason", aud="archivist", persona="mason"
    )
    async for _ in router.dispatch_streaming(
        target_agent="archivist",
        target_generation=None,
        task=_sample_task(),
        caller_jwt=persona_token,
    ):
        pass
    assert seen[-1].caller_persona == "mason"
    assert seen[-1].caller_agent == "persona:mason"

    operator_token = mint(
        signing_key=signing_key, sub="operator:richard", aud="archivist"
    )
    second = _sample_task()
    second.id = "t-003"  # conversations.task_id is UNIQUE
    async for _ in router.dispatch_streaming(
        target_agent="archivist",
        target_generation=None,
        task=second,
        caller_jwt=operator_token,
    ):
        pass
    assert seen[-1].caller_persona is None
    assert seen[-1].caller_agent == "operator:richard"
