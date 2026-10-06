"""cabal#154 — delegation is permitted where an edge was declared, and nowhere else."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from cabal.config.identity import GuardrailsConfig, IdentityConfig, PersonaConfig
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import (
    _caller_edges,
    _delegation_edges,
    build_router_input_guardrails,
    create_app,
)
from cabal.core.auth.jwt import AGENT_SUB_PREFIX, mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import DelegationEdgeGuardrail

# cabal#152 — these guards now return TERMINATE, not BLOCK.
#
# The change is semantic, not cosmetic, and updating the assertion is the
# point rather than collateral. A BLOCK advises and lets the task continue;
# in the storm behind #146 an agent took that advice ~270 times, because an
# agent with no terminal state has exactly one move when stuck. Neither of
# these refusals can be rephrased past: an edge is operator config and a
# spent budget is spent. So they END the task.
#
# The budget previously reached terminal behaviour by ACCIDENT -- its counter
# only rises, so once over budget every later delegation blocked too. That was
# a workaround exploiting monotonicity, not a terminal state.


def _task(target_agent: str | None = "archivist") -> Task:
    t = Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="a question")]),
        ),
    )
    if target_agent is not None:
        t.metadata["cabal_target_agent"] = target_agent
    return t


def _ctx(
    caller_persona: str | None, caller_agent: str | None = "operator:richard"
) -> GuardrailContext:
    return GuardrailContext(
        persona_name=caller_persona or (caller_agent or ""),
        task_id="t-1",
        phase="router",
        caller_persona=caller_persona,
        caller_agent=caller_agent,
    )


@pytest.mark.asyncio
async def test_a_declared_edge_permits_the_delegation() -> None:
    g = DelegationEdgeGuardrail(edges={"mason": ["archivist"]})
    assert (await g.check(_task("archivist"), _ctx("mason"))).type is VerdictType.PASS


@pytest.mark.asyncio
async def test_an_undeclared_edge_refuses_before_dispatch() -> None:
    """THE ACCEPTANCE CRITERION: refused, with a reason naming the missing edge."""
    g = DelegationEdgeGuardrail(edges={"mason": ["archivist"]})
    v = await g.check(_task("librarian"), _ctx("mason"))
    assert v.type is VerdictType.TERMINATE
    assert v.reason is not None
    assert "mason" in v.reason and "librarian" in v.reason
    assert "may_delegate_to" in v.reason, "the refusal must name the remedy"


@pytest.mark.asyncio
async def test_a_room_with_no_declared_edges_permits_no_delegation() -> None:
    """The load-bearing half of the borrowed pattern. A permissive default here
    would make this a suggestion rather than an authorization control."""
    g = DelegationEdgeGuardrail(edges={})
    v = await g.check(_task("archivist"), _ctx("mason"))
    assert v.type is VerdictType.TERMINATE
    assert v.reason is not None and "nothing" in v.reason


@pytest.mark.asyncio
async def test_the_operator_is_not_subject_to_edges() -> None:
    """Edges govern agent-to-agent delegation; a human at the CLI is not
    delegating. The exemption is granted on a POSITIVE signal -- an
    `operator:` sub -- never on the absence of a persona claim."""
    g = DelegationEdgeGuardrail(edges={})
    v = await g.check(_task("archivist"), _ctx(None, "operator:richard"))
    assert v.type is VerdictType.PASS


@pytest.mark.asyncio
async def test_a_personaless_service_token_is_refused_not_exempted() -> None:
    """THE PRE-PASS FINDING ON b606317, and the reason the exemption is now
    positive rather than inferred.

    The first version read "no cabal_persona claim" as "operator", justified by
    routes/auth.py being the only mint site producing a claim-less token. It is
    not: admin.py's mint_service_token takes an optional `persona` defaulting
    to None. A service token was therefore claim-less AND exempt -- a bypass of
    the whole control, arrived at by trusting an enumeration.

    A default-deny control must not depend on a list of exceptions being
    complete. This is the test that makes the dependency go the other way: an
    unrecognised caller shape fails CLOSED.
    """
    g = DelegationEdgeGuardrail(edges={"mason": ["archivist"]})
    v = await g.check(_task("archivist"), _ctx(None, "svc:indexer"))
    assert v.type is VerdictType.TERMINATE
    assert v.reason is not None
    assert "neither an identified persona nor the operator" in v.reason


@pytest.mark.asyncio
async def test_a_caller_with_no_identity_at_all_is_refused() -> None:
    g = DelegationEdgeGuardrail(edges={})
    assert (await g.check(_task("archivist"), _ctx(None, None))).type is VerdictType.TERMINATE


@pytest.mark.asyncio
async def test_an_unidentifiable_target_is_refused_not_waved_through() -> None:
    """A default-deny control that fails open on a missing field is not one."""
    g = DelegationEdgeGuardrail(edges={"mason": ["archivist"]})
    v = await g.check(_task(target_agent=None), _ctx("mason"))
    assert v.type is VerdictType.TERMINATE
    assert v.reason is not None and "could not be identified" in v.reason


@pytest.mark.asyncio
async def test_an_edge_cannot_be_supplied_by_the_agent() -> None:
    """The property that makes this authorization rather than advice. The guard
    holds an immutable snapshot built from config at startup; nothing reachable
    from the dispatch path can add an edge."""
    g = DelegationEdgeGuardrail(edges={"mason": ["archivist"]})
    task = _task("librarian")
    # every lever an agent has: its own task metadata
    task.metadata["may_delegate_to"] = ["librarian"]
    task.metadata["cabal_edges"] = {"mason": ["librarian"]}
    assert (await g.check(task, _ctx("mason"))).type is VerdictType.TERMINATE
    assert g.edges_for("mason") == frozenset({"archivist"})


@pytest.mark.asyncio
async def test_a_non_task_target_passes() -> None:
    g = DelegationEdgeGuardrail(edges={})
    assert (await g.check("some text", _ctx("mason"))).type is VerdictType.PASS


def test_check_is_await_free() -> None:
    """Same invariant the other two router guards carry: a primus fan-out calls
    these concurrently across legs sharing one instance, and an await-free
    `async def` runs atomically to completion on a cooperative event loop."""
    import inspect

    src = inspect.getsource(DelegationEdgeGuardrail.check)
    assert "await " not in src


# ---- the edge map: personas in, AGENT names out --------------------------


def test_an_edge_to_a_primus_resolves_to_its_agent_name() -> None:
    """Edges are declared against PERSONAS; `cabal_target_agent` carries an
    AGENT name, and identity_sync gives a role="primus" persona the name
    "<persona>-primus". Resolving once at startup beats stripping a suffix on
    the hot path, which would authorize a persona literally named "x-primus"
    as though it were the primus of "x"."""
    ident = IdentityConfig(
        personas={
            "mason": PersonaConfig(role="primus"),
            "archivist": PersonaConfig(role="singleton", may_delegate_to=["mason"]),
        }
    )
    assert _delegation_edges(ident)["archivist"] == frozenset({"mason-primus"})


def test_an_unknown_target_name_passes_through_verbatim() -> None:
    """So an edge to a federated peer, which has no local persona, is expressible."""
    ident = IdentityConfig(
        personas={"mason": PersonaConfig(role="primus", may_delegate_to=["far-peer"])}
    )
    assert _delegation_edges(ident)["mason"] == frozenset({"far-peer"})


# ---- wiring --------------------------------------------------------------


def _personas() -> dict[str, PersonaConfig]:
    return {
        "mason": PersonaConfig(role="primus", may_delegate_to=["archivist"]),
        "archivist": PersonaConfig(role="singleton"),
    }


def test_the_edge_guard_is_off_by_default() -> None:
    """OPT-IN, unlike its two neighbours. Every room that exists today has no
    edges declared, so a default-on switch would turn all delegation off at
    upgrade with no warning. The default-DENY is inside the guard; the
    migration is this flag."""
    names = [
        type(g).__name__
        for g in build_router_input_guardrails(IdentityConfig(personas=_personas()))
    ]
    assert "DelegationEdgeGuardrail" not in names
    assert "DoomLoopGuardrail" in names, "the opt-in must not disturb its neighbours"


def test_the_edge_guard_is_wired_with_its_configured_edges() -> None:
    ident = IdentityConfig(
        personas=_personas(), guardrails=GuardrailsConfig(delegation_edges=True)
    )
    edge_guards = [
        g
        for g in build_router_input_guardrails(ident)
        if isinstance(g, DelegationEdgeGuardrail)
    ]
    assert len(edge_guards) == 1
    assert edge_guards[0].edges_for("mason") == frozenset({"archivist"})


# ---- agent:<name> subs as callers (cabal#189) ----------------------------


@pytest.mark.asyncio
async def test_a_colleague_with_a_declared_sub_edge_may_delegate() -> None:
    """A colleague's token carries sub="agent:<name>" and no cabal_persona.
    Before #189 every such caller was refused once edges were on."""
    g = DelegationEdgeGuardrail(edges={}, caller_edges={"agent:ward": ["archivist"]})
    v = await g.check(_task("archivist"), _ctx(None, "agent:ward"))
    assert v.type is VerdictType.PASS


@pytest.mark.asyncio
async def test_a_colleague_with_no_sub_edge_is_refused_with_the_remedy() -> None:
    g = DelegationEdgeGuardrail(edges={}, caller_edges={"agent:ward": ["archivist"]})
    v = await g.check(_task("librarian"), _ctx(None, "agent:ward"))
    assert v.type is VerdictType.TERMINATE
    assert v.reason is not None
    assert "'agent:ward' to 'librarian'" in v.reason
    assert '"agent:ward" under [guardrails.caller_edges]' in v.reason


@pytest.mark.asyncio
async def test_an_undeclared_sub_may_delegate_to_nobody() -> None:
    """Same default-deny as a persona with no may_delegate_to."""
    g = DelegationEdgeGuardrail(edges={}, caller_edges={"agent:ward": ["archivist"]})
    v = await g.check(_task("archivist"), _ctx(None, "agent:ext-acme"))
    assert v.type is VerdictType.TERMINATE
    assert v.reason is not None and "may delegate to: nothing" in v.reason


@pytest.mark.asyncio
async def test_a_sub_named_like_a_persona_does_not_inherit_its_edges() -> None:
    """THE REASON THE PREFIX IS KEPT. routes/auth.py strips agent: to get a
    persona name for the liminal exchange; doing that here would hand an
    outsider or colleague named `archivist` the local archivist's edges."""
    g = DelegationEdgeGuardrail(edges={"archivist": ["librarian"]}, caller_edges={})
    v = await g.check(_task("librarian"), _ctx(None, "agent:archivist"))
    assert v.type is VerdictType.TERMINATE


@pytest.mark.asyncio
async def test_a_persona_claim_is_judged_as_the_persona_whatever_the_sub() -> None:
    g = DelegationEdgeGuardrail(
        edges={"mason": ["archivist"]}, caller_edges={"agent:mason": ["librarian"]}
    )
    ctx = _ctx("mason", "agent:mason")
    assert (await g.check(_task("archivist"), ctx)).type is VerdictType.PASS
    assert (await g.check(_task("librarian"), ctx)).type is VerdictType.TERMINATE


@pytest.mark.asyncio
async def test_a_bare_agent_prefix_is_not_an_identity() -> None:
    g = DelegationEdgeGuardrail(edges={}, caller_edges={})
    v = await g.check(_task("archivist"), _ctx(None, "agent:"))
    assert v.type is VerdictType.TERMINATE
    assert v.reason is not None
    assert "neither an identified persona nor the operator" in v.reason


@pytest.mark.parametrize("key", ["archivist", "operator:richard", "agent:", "svc:x"])
def test_caller_edges_keys_must_be_agent_subs(key: str) -> None:
    """A persona's edges belong in may_delegate_to; a key here that is not a
    full agent sub is a namespace mix-up, refused at load."""
    with pytest.raises(ValueError, match="caller_edges keys"):
        GuardrailsConfig(caller_edges={key: ["archivist"]})


def test_the_config_literal_matches_the_jwt_constant() -> None:
    """config/identity.py cannot import core and repeats "agent:"."""
    GuardrailsConfig(caller_edges={f"{AGENT_SUB_PREFIX}ward": []})
    with pytest.raises(ValueError):
        GuardrailsConfig(caller_edges={AGENT_SUB_PREFIX: []})


def test_caller_edge_targets_resolve_like_persona_edges() -> None:
    ident = IdentityConfig(
        personas={"mason": PersonaConfig(role="primus")},
        guardrails=GuardrailsConfig(caller_edges={"agent:ward": ["mason", "far-peer"]}),
    )
    assert _caller_edges(ident) == {"agent:ward": frozenset({"mason-primus", "far-peer"})}


@pytest.mark.asyncio
async def test_the_edge_guard_is_wired_with_its_caller_edges() -> None:
    ident = IdentityConfig(
        personas=_personas(),
        guardrails=GuardrailsConfig(
            delegation_edges=True, caller_edges={"agent:ward": ["archivist"]}
        ),
    )
    (g,) = [
        g
        for g in build_router_input_guardrails(ident)
        if isinstance(g, DelegationEdgeGuardrail)
    ]
    assert (await g.check(_task("archivist"), _ctx(None, "agent:ward"))).type is VerdictType.PASS


# ---- end to end: real tokens through the real routes and router ---------


@asynccontextmanager
async def _app_with_edges() -> AsyncIterator[tuple[AsyncClient, Callable[..., str]]]:
    """Boot the app on an identity.toml with edges on and one sub edge each
    for a room colleague and an outside agent, both to the built-in echo."""
    Path("identity.toml").write_text(
        "[guardrails]\n"
        "delegation_edges = true\n"
        "[guardrails.caller_edges]\n"
        '"agent:ward" = ["echo"]\n'
        '"agent:ext-partner" = ["echo"]\n'
    )
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    app = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    async with app.router.lifespan_context(app):
        async with open_session(app.state.sessionmaker) as session:
            stored = await load_active_signing_key(session, master_key=app.state.master_key)
        assert stored is not None

        def token(**kw: object) -> str:
            return mint(signing_key=stored, **kw)  # type: ignore[arg-type]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, token


async def _colleague_dispatch(sub: str, task_id: str) -> Response:
    """A colleague pass as the room service mints it: aud=cabal-core,
    scope=colleague, sub=agent:<name>, no cabal_persona. Re-minted per target."""
    async with _app_with_edges() as (client, token):
        bearer = token(sub=sub, aud="cabal-core", scope=["colleague"])
        return await client.post(
            "/a2a/tasks",
            json={
                "target": "echo",
                "task": {
                    "id": task_id,
                    "status": {
                        "state": "submitted",
                        "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
                    },
                },
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )


async def _outsider_send(sub: str) -> Response:
    """An outside agent's per-persona token as docs/outside-agents.md has the
    operator mint it: aud=<agent>, scope=dispatch, sub=agent:ext-<name>."""
    async with _app_with_edges() as (client, token):
        bearer = token(sub=sub, aud="echo", scope=["dispatch"])
        return await client.post(
            "/agents/echo/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {
                    "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
                },
            },
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {bearer}"},
        )


@pytest.mark.asyncio
async def test_a_colleague_with_a_declared_edge_reaches_its_target_end_to_end() -> None:
    """THE BUG IN #189: with edges on, this colleague dispatch was refused
    because the router saw caller_persona=None and a non-operator sub."""
    r = await _colleague_dispatch("agent:ward", "t-189-ok")
    assert r.status_code == 200, r.text
    assert r.json()["status"]["state"] == "completed", r.text


@pytest.mark.asyncio
async def test_a_colleague_without_an_edge_is_refused_end_to_end() -> None:
    r = await _colleague_dispatch("agent:stranger", "t-189-no")
    assert r.status_code == 403, r.text
    assert "no declared edge from 'agent:stranger' to 'echo'" in r.json()["detail"], r.text


@pytest.mark.asyncio
async def test_an_outside_agent_with_a_declared_edge_reaches_its_target() -> None:
    """Same gap, second caller kind: an outsider's token has no persona either."""
    r = await _outsider_send("agent:ext-partner")
    body = r.json()
    assert "result" in body, body
    assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", body


@pytest.mark.asyncio
async def test_an_outside_agent_without_an_edge_is_refused() -> None:
    r = await _outsider_send("agent:ext-stranger")
    assert r.status_code == 403, r.text
    assert "no declared edge from 'agent:ext-stranger' to 'echo'" in r.json()["detail"], r.text
