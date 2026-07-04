"""Unit and integration tests for DoomLoopGuardrail — cabal#42.

Coverage:
  - N-1 identical check() calls return PASS; Nth returns BLOCK with reason.
  - Different payloads don't trip the counter.
  - Different target agents don't trip the counter.
  - Two different session keys keep independent counters.
  - A non-identical delegation resets the count.
  - Threshold is configurable (tested with threshold=2).
  - Router integration: register guardrail, dispatch same Task N times,
    assert the Nth dispatch is a policy-violation (blocked).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.a2a.codes import CODE_POLICY_VIOLATION, reason_code
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import create_signing_key
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.broker.router import DispatchRouter
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import DoomLoopGuardrail
from cabal.core.models import Base, Persona

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    text: str,
    context_id: str | None = "ctx-1",
    target_agent: str = "target",
    task_id: str = "t-1",
) -> Task:
    """Build a minimal Task with the doom-loop metadata already set.

    ``cabal_target_agent`` is normally written by the router before
    guardrails run; in standalone unit tests we set it directly.

    Note: ``context_id`` must be passed via its Pydantic alias ``contextId``
    because ``_Open`` does not enable ``populate_by_name=True``.  When
    ``context_id`` is None the guardrail falls back to ``task.id``.
    """
    kwargs: dict = {
        "id": task_id,
        "status": TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
        "metadata": {"cabal_target_agent": target_agent},
    }
    if context_id is not None:
        kwargs["contextId"] = context_id
    return Task(**kwargs)


def _ctx() -> GuardrailContext:
    return GuardrailContext(persona_name="caller", task_id="t-1", phase="router")


# ---------------------------------------------------------------------------
# 1. Unit tests (guardrail in isolation — no router needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_n_minus_1_identical_returns_pass_nth_returns_block() -> None:
    """Default threshold=3: first two identical calls pass; third blocks."""
    g = DoomLoopGuardrail(threshold=3)
    task = _task("do the thing")
    ctx = _ctx()

    v1 = await g.check(task, ctx)
    assert v1.type == VerdictType.PASS

    v2 = await g.check(task, ctx)
    assert v2.type == VerdictType.PASS

    v3 = await g.check(task, ctx)
    assert v3.type == VerdictType.BLOCK
    assert "3 identical delegations" in v3.reason
    assert "target" in v3.reason
    assert "pausing" in v3.reason
    assert "change approach" in v3.reason


@pytest.mark.asyncio
async def test_different_payloads_do_not_trip_counter() -> None:
    """Distinct message texts never accumulate toward the threshold."""
    g = DoomLoopGuardrail(threshold=3)
    ctx = _ctx()

    for i in range(6):
        task = _task(f"unique message {i}", context_id="sess-payloads")
        v = await g.check(task, ctx)
        assert v.type == VerdictType.PASS, f"call {i} unexpectedly blocked"


@pytest.mark.asyncio
async def test_different_target_agents_do_not_trip_counter() -> None:
    """Same payload to different targets each have their own delegation key."""
    g = DoomLoopGuardrail(threshold=3)
    ctx = _ctx()

    for i in range(6):
        task = _task("same payload", context_id="sess-targets", target_agent=f"agent-{i}")
        v = await g.check(task, ctx)
        assert v.type == VerdictType.PASS, f"call {i} unexpectedly blocked"


@pytest.mark.asyncio
async def test_two_session_keys_keep_independent_counters() -> None:
    """Sessions A and B accumulate independently — A blocking does not affect B.

    We interleave calls and verify that:
    - sess-A reaches the threshold and blocks.
    - sess-B, which has received fewer calls, is still safe (does not block
      because of sess-A's overflow).
    """
    g = DoomLoopGuardrail(threshold=3)
    ctx = _ctx()

    # sess-A: 2 calls (count=2, still safe).
    for _ in range(2):
        v = await g.check(_task("same", context_id="sess-A"), ctx)
        assert v.type == VerdictType.PASS

    # sess-B: 1 call (count=1, independent counter).
    v = await g.check(_task("same", context_id="sess-B"), ctx)
    assert v.type == VerdictType.PASS

    # sess-A: 3rd call — reaches threshold, BLOCK.
    v = await g.check(_task("same", context_id="sess-A"), ctx)
    assert v.type == VerdictType.BLOCK

    # sess-B: 2nd call — counter is independently at 2 (not 3+), still PASS.
    v = await g.check(_task("same", context_id="sess-B"), ctx)
    assert v.type == VerdictType.PASS


@pytest.mark.asyncio
async def test_non_identical_delegation_resets_count() -> None:
    """A different delegation resets the counter; resuming identical afterwards
    re-starts from 1, not from the pre-reset count."""
    g = DoomLoopGuardrail(threshold=3)
    ctx = _ctx()

    # Two identical — counter at 2.
    for _ in range(2):
        v = await g.check(_task("msg-A", context_id="sess-reset"), ctx)
        assert v.type == VerdictType.PASS

    # Different message — resets counter to 1 (PASS).
    v = await g.check(_task("msg-B", context_id="sess-reset"), ctx)
    assert v.type == VerdictType.PASS

    # Back to msg-A: counter starts fresh at 1; two more are safe.
    for _ in range(2):
        v = await g.check(_task("msg-A", context_id="sess-reset"), ctx)
        assert v.type == VerdictType.PASS

    # Now at count=3 → BLOCK.
    v = await g.check(_task("msg-A", context_id="sess-reset"), ctx)
    assert v.type == VerdictType.BLOCK


@pytest.mark.asyncio
async def test_configurable_threshold_two() -> None:
    """threshold=2: first call passes, second blocks."""
    g = DoomLoopGuardrail(threshold=2)
    ctx = _ctx()
    task = _task("msg", context_id="sess-thresh")

    v1 = await g.check(task, ctx)
    assert v1.type == VerdictType.PASS  # count=1

    v2 = await g.check(task, ctx)
    assert v2.type == VerdictType.BLOCK  # count=2 >= threshold=2
    assert "2 identical delegations" in v2.reason


@pytest.mark.asyncio
async def test_non_task_target_always_passes() -> None:
    """Guardrail is a no-op for non-Task targets (e.g. str from output phase)."""
    g = DoomLoopGuardrail(threshold=2)
    ctx = GuardrailContext(persona_name="p", task_id="t", phase="output")

    for _ in range(5):
        v = await g.check("some output text", ctx)
        assert v.type == VerdictType.PASS


@pytest.mark.asyncio
async def test_block_persists_then_resets_on_different_delegation() -> None:
    """After blocking, subsequent identical calls keep blocking; a different
    delegation resets and the next identical sequence starts fresh."""
    g = DoomLoopGuardrail(threshold=2)
    ctx = _ctx()

    # Trip the block.
    await g.check(_task("msg", context_id="sess-persist"), ctx)
    v_block = await g.check(_task("msg", context_id="sess-persist"), ctx)
    assert v_block.type == VerdictType.BLOCK

    # Still identical — block continues.
    v_still = await g.check(_task("msg", context_id="sess-persist"), ctx)
    assert v_still.type == VerdictType.BLOCK

    # Different delegation — resets.
    v_reset = await g.check(_task("other", context_id="sess-persist"), ctx)
    assert v_reset.type == VerdictType.PASS

    # Identical again from count=1 — safe.
    v_safe = await g.check(_task("msg", context_id="sess-persist"), ctx)
    assert v_safe.type == VerdictType.PASS


def test_threshold_zero_or_negative_raises() -> None:
    """threshold < 1 is meaningless — constructor rejects it."""
    with pytest.raises(ValueError, match="threshold"):
        DoomLoopGuardrail(threshold=0)

    with pytest.raises(ValueError, match="threshold"):
        DoomLoopGuardrail(threshold=-1)


# ---------------------------------------------------------------------------
# 2. Router integration tests
#
# Fixtures are duplicated from tests/test_guardrails.py for isolation (that
# file itself notes it duplicated them from test_broker_router.py).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest.fixture
def _master_key() -> bytes:
    return Fernet.generate_key()


@pytest_asyncio.fixture
async def _signing_key(_sessionmaker, _master_key):
    async with _sessionmaker() as s:
        stored = await create_signing_key(s, master_key=_master_key)
        await s.commit()
    return stored


@pytest.fixture
def _jwks_provider(_signing_key):
    jwk_dict = dict(
        RSAKey.import_key(_signing_key.private_key.public_key()).as_dict(private=False)
    )
    jwk_dict["kid"] = _signing_key.kid
    key_set = KeySet.import_key_set({"keys": [jwk_dict]})
    return lambda: key_set


@pytest_asyncio.fixture
async def _agent_row(_sessionmaker):
    async with _sessionmaker() as s:
        s.add(Persona(name="echo", config_json={}))
        await s.flush()
        row = await bulletin_store.register_agent(
            s,
            name="echo",
            persona_name="echo",
            role="singleton",
            endpoint="http://echo:7400",
            capabilities={"skills": []},
            safety_tier="strict",
            agent_card_json={"name": "echo"},
            agent_card_kid="kid-1",
        )
        await s.commit()
    return row


@dataclass(slots=True)
class _EchoHandler:
    name: str = "echo"
    dispatched: list[Task] = field(default_factory=list)

    async def handle_task(self, task: Task) -> Task:
        self.dispatched.append(task)
        return Task(
            id=task.id,
            status=TaskStatus(
                state="completed",
                message=Message(role="agent", parts=[TextPart(text="echoed")]),
            ),
            metadata=task.metadata,
        )


def _dispatch_task(task_id: str, context_id: str = "conv-1") -> Task:
    """A Task with a stable context_id so repeated dispatches share a session key.

    Each dispatch needs a unique task_id (bookkeeper unique constraint), but
    the shared context_id keeps the DoomLoopGuardrail's session key stable.
    """
    return Task(
        **{
            "id": task_id,
            "contextId": context_id,
            "status": TaskStatus(
                state="submitted",
                message=Message(role="user", parts=[TextPart(text="do the thing")]),
            ),
        }
    )


@pytest.mark.asyncio
async def test_router_doom_loop_blocks_on_nth_dispatch(
    _sessionmaker, _signing_key, _jwks_provider, _agent_row
) -> None:
    """DoomLoopGuardrail wired into the router blocks the Nth identical dispatch.

    threshold=2: first dispatch passes, second is blocked (policy-violation).
    The router sets cabal_target_agent in metadata before calling run_guardrails,
    so the guardrail correctly identifies the target as "echo".

    Each dispatch has a unique task_id (bookkeeper UNIQUE constraint) but
    shares context_id="conv-loop" so the guardrail sees the same session key.
    """
    guardrail = DoomLoopGuardrail(threshold=2)
    router = DispatchRouter(
        sessionmaker=_sessionmaker,
        bookkeeper=Bookkeeper(_sessionmaker),
        jwks_provider=_jwks_provider,
        input_guardrails=[guardrail],
    )
    handler = _EchoHandler()
    router.register_local(handler)

    token = mint(signing_key=_signing_key, sub="primus:1", aud="echo")

    # First dispatch — count=1, PASS, handler reached.
    r1 = await router.dispatch(
        target_agent="echo",
        target_generation=None,
        task=_dispatch_task(task_id="r-loop-01", context_id="conv-loop"),
        caller_jwt=token,
    )
    assert r1.status.state == "completed"
    assert len(handler.dispatched) == 1

    # Second dispatch (different task_id, same context_id + same payload) → count=2 >= 2 → BLOCK.
    r2 = await router.dispatch(
        target_agent="echo",
        target_generation=None,
        task=_dispatch_task(task_id="r-loop-02", context_id="conv-loop"),
        caller_jwt=token,
    )
    assert r2.status.state == "failed"
    assert reason_code(r2) == CODE_POLICY_VIOLATION
    reason_msg = r2.metadata.get("cabal_reason_message") or ""
    assert "router guardrail blocked" in reason_msg
    assert "identical delegations" in reason_msg
    assert "echo" in reason_msg
    # Handler was NOT called a second time.
    assert len(handler.dispatched) == 1


@pytest.mark.asyncio
async def test_router_doom_loop_different_payload_does_not_block(
    _sessionmaker, _signing_key, _jwks_provider, _agent_row
) -> None:
    """Different payloads on the same session never trip the counter."""
    guardrail = DoomLoopGuardrail(threshold=2)
    router = DispatchRouter(
        sessionmaker=_sessionmaker,
        bookkeeper=Bookkeeper(_sessionmaker),
        jwks_provider=_jwks_provider,
        input_guardrails=[guardrail],
    )
    handler = _EchoHandler()
    router.register_local(handler)

    token = mint(signing_key=_signing_key, sub="primus:1", aud="echo")

    for i in range(4):
        task = Task(
            **{
                "id": f"r-vary-{i}",
                "contextId": "conv-vary",
                "status": TaskStatus(
                    state="submitted",
                    message=Message(role="user", parts=[TextPart(text=f"unique payload {i}")]),
                ),
            }
        )
        r = await router.dispatch(
            target_agent="echo",
            target_generation=None,
            task=task,
            caller_jwt=token,
        )
        assert r.status.state == "completed", f"dispatch {i} unexpectedly blocked"

    assert len(handler.dispatched) == 4


@pytest.mark.asyncio
async def test_distinct_task_ids_without_context_id_never_block() -> None:
    """Documents the known limitation (cabal#59): without a stable shared
    session key, the detector cannot group re-delegations. Fresh task ids and
    NO context_id — the production shape of the rook fan-out and A2A inbound
    paths — means every check is a new session, so the counter never trips."""
    g = DoomLoopGuardrail(threshold=3)
    ctx = _ctx()
    # Byte-identical delegation, but each hop is a fresh task id with no
    # context_id — exactly what bypasses the counter today.
    for i in range(6):
        task = _task("identical payload", context_id=None, task_id=f"fresh-{i}")
        v = await g.check(task, ctx)
        assert v.type == VerdictType.PASS, f"iteration {i} unexpectedly blocked"


@pytest.mark.asyncio
async def test_non_serialisable_data_part_does_not_crash() -> None:
    """A DataPart whose payload isn't JSON-serialisable must not raise out of
    check() and crash the dispatch — it falls back to repr() (Prudence nit)."""
    g = DoomLoopGuardrail(threshold=2)
    ctx = _ctx()

    class _Weird:
        def __repr__(self) -> str:
            return "weird"

    task = _task("x")
    # Force a non-serialisable data part onto the message.
    from cabal.core.a2a.types import DataPart

    task.status.message.parts = [DataPart(data={"obj": _Weird()})]
    # Must return a Verdict (not raise); two identical → block at threshold=2.
    v1 = await g.check(task, ctx)
    assert v1.type == VerdictType.PASS
    v2 = await g.check(task, ctx)
    assert v2.type == VerdictType.BLOCK
