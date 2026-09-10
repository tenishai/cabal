"""cabal#76 — the production DispatchRouter wires a default DoomLoopGuardrail as
a router-level input guardrail, so FEDERATED dispatch is doom-loop protected.

Background: the router runs ``input_guardrails`` in ``dispatch()`` BEFORE the
local-vs-remote branch, so it is the one enforcement point that also covers
``kind="federated"`` peers. Pre-#76 the production router (app.py) constructed
with none — a delegation to a federated peer got JWT auth + bookkeeping +
AbortGraph but ZERO guardrails. The in-process Mason ``DoomLoopGuardrail``
(primus.py) only fires at the subordinate boundary and never reaches a remote
hop.

Three layers:
  A. WIRING FENCE — the create_app-built router (``app.state.dispatch_router``)
     carries a DoomLoopGuardrail by default. Fails on pre-#76 code (empty list).
  B. HELPER — ``build_router_input_guardrails``: default-on, threshold honored,
     opt-out via config yields no guardrails.
  C. BEHAVIORAL — a repeated identical delegation to a ``kind="federated"``
     target is BLOCKED before the remote hop (the issue's core concern).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.config.identity import GuardrailsConfig, IdentityConfig
from cabal.core.a2a.codes import CODE_POLICY_VIOLATION, reason_code
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import build_router_input_guardrails
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import create_signing_key
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.broker.router import DispatchRouter
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.guardrails_builtin import (
    DelegationBudgetGuardrail,
    DoomLoopGuardrail,
)
from cabal.core.models import Base, Persona

# ======================================================================
# A. WIRING FENCE — the production router has the guardrail by default
# ======================================================================


@pytest.mark.asyncio
async def test_production_router_wires_doom_loop_by_default(app: FastAPI) -> None:
    """create_app must construct the DispatchRouter WITH a DoomLoopGuardrail.

    Fail-without-fix: pre-#76 app.py passed no ``input_guardrails``, so
    ``_input_guardrails == []`` and this assertion fails — leaving federated
    dispatch unguarded.
    """
    router = app.state.dispatch_router
    assert any(
        isinstance(g, DoomLoopGuardrail) for g in router._input_guardrails
    ), "production router has no DoomLoopGuardrail — federated dispatch is unguarded"


# ======================================================================
# B. HELPER — default-on, threshold from config, opt-out
# ======================================================================


def test_build_router_input_guardrails_default_on() -> None:
    """No identity (no identity.toml) → default-on DoomLoop at threshold 5.

    The default is 5 (looser than the in-process Mason guard's 3) because this
    guard runs on ALL production dispatch, human cbl chat turns included.
    """
    dooms = [
        g for g in build_router_input_guardrails(None) if isinstance(g, DoomLoopGuardrail)
    ]
    assert len(dooms) == 1
    assert dooms[0]._threshold == 5


def test_guardrails_config_rejects_threshold_below_one() -> None:
    """A bad doom_loop_threshold surfaces a field-named config error at load,
    not a raw ValueError deep in create_app router construction."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="doom_loop_threshold"):
        GuardrailsConfig(doom_loop_threshold=0)


def test_build_router_input_guardrails_threshold_from_config() -> None:
    identity = IdentityConfig(guardrails=GuardrailsConfig(doom_loop_threshold=5))
    dooms = [
        g
        for g in build_router_input_guardrails(identity)
        if isinstance(g, DoomLoopGuardrail)
    ]
    assert len(dooms) == 1
    assert dooms[0]._threshold == 5


def test_build_router_input_guardrails_opt_out() -> None:
    """``doom_loop = false`` removes the doom-loop guard.

    It no longer empties the list: cabal#146 added a second, independent
    guard, and the two opt out separately on purpose -- turning off the
    sameness counter must not silently disable the delegation budget.
    """
    identity = IdentityConfig(guardrails=GuardrailsConfig(doom_loop=False))
    wired = build_router_input_guardrails(identity)
    assert not [g for g in wired if isinstance(g, DoomLoopGuardrail)]
    assert [g for g in wired if isinstance(g, DelegationBudgetGuardrail)]


def test_both_guards_opt_out_independently() -> None:
    identity = IdentityConfig(
        guardrails=GuardrailsConfig(doom_loop=False, delegation_budget=False)
    )
    assert build_router_input_guardrails(identity) == []


def test_delegation_budget_is_wired_with_its_configured_limit() -> None:
    identity = IdentityConfig(
        guardrails=GuardrailsConfig(delegation_budget_limit=7)
    )
    budgets = [
        g
        for g in build_router_input_guardrails(identity)
        if isinstance(g, DelegationBudgetGuardrail)
    ]
    assert len(budgets) == 1
    assert budgets[0].limit == 7


def test_both_guards_are_wired_by_default() -> None:
    """Default-on for both. A deployment that never edits identity.toml gets
    the paraphrase bound as well as the sameness bound."""
    wired = build_router_input_guardrails(IdentityConfig())
    kinds = {type(g).__name__ for g in wired}
    assert kinds == {"DoomLoopGuardrail", "DelegationBudgetGuardrail"}


# ======================================================================
# C. BEHAVIORAL — a federated delegation loop is BLOCKED before the hop
# ======================================================================

# fixtures mirror test_broker_router.py for isolation


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


def _delegation(hop_id: str) -> Task:
    """An identical delegation under a SHARED session key (context_id).

    Same message payload + same target + shared context_id → the DoomLoop
    delegation key recurs and its cumulative count accumulates across hops.
    Each hop still carries a distinct task id (a fresh A2A envelope).
    """
    task = Task(
        id=hop_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="delegate the same thing")]),
        ),
    )
    task.context_id = "ctx-doom"  # shared session key across hops
    return task


@pytest.mark.asyncio
async def test_federated_dispatch_loop_is_blocked_before_remote_hop(
    sessionmaker, signing_key, jwks_provider
) -> None:
    """A repeated identical delegation to a ``kind="federated"`` peer trips the
    router DoomLoop and is BLOCKED before ``_dispatch_remote`` — the exact gap
    #76 closes. With threshold=2: hop 1 reaches the remote; hop 2 (identical) is
    blocked, so the remote is invoked exactly once.
    """
    # Seed a federated target row (a sovereign peer we're a client of).
    async with sessionmaker() as s:
        s.add(Persona(name="scout", config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name="scout",
            persona_name="scout",
            role="singleton",
            endpoint="http://scout:7400",
            capabilities={"skills": []},
            safety_tier="strict",
            agent_card_json={"name": "scout"},
            agent_card_kid="kid-1",
            kind="federated",
            federation_audience="scout-svc",
        )
        await s.commit()

    router = DispatchRouter(
        sessionmaker=sessionmaker,
        bookkeeper=Bookkeeper(sessionmaker),
        jwks_provider=jwks_provider,
        input_guardrails=[DoomLoopGuardrail(threshold=2)],
    )

    # Spy the remote hop so no real HTTP happens; it returns a completed Task.
    async def _fake_remote(target, task, caller_jwt):
        return Task(
            id=task.id, status=TaskStatus(state="completed"), metadata=task.metadata
        )

    remote = AsyncMock(side_effect=_fake_remote)
    router._dispatch_remote = remote  # type: ignore[assignment]

    # aud must match the federated row's federation_audience (verified in _prepare).
    token = mint(signing_key=signing_key, sub="primus:1", aud="scout-svc")

    # hop 1 — reaches the federated peer over the (mocked) remote path.
    r1 = await router.dispatch(
        target_agent="scout",
        target_generation=None,
        task=_delegation("hop-1"),
        caller_jwt=token,
    )
    assert r1.status.state == "completed"
    assert remote.await_count == 1

    # hop 2 — identical delegation, same context_id → BLOCKED before the hop.
    r2 = await router.dispatch(
        target_agent="scout",
        target_generation=None,
        task=_delegation("hop-2"),
        caller_jwt=token,
    )
    assert reason_code(r2) == CODE_POLICY_VIOLATION
    assert "router guardrail blocked" in (r2.metadata.get("cabal_reason_message") or "")
    # The federated hop was NOT taken the second time — the guard covers remote.
    assert remote.await_count == 1
