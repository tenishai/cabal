"""cabal#154 — delegation is permitted where an edge was declared, and nowhere else."""

from __future__ import annotations

import pytest

from cabal.config.identity import GuardrailsConfig, IdentityConfig, PersonaConfig
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import _delegation_edges, build_router_input_guardrails
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import DelegationEdgeGuardrail


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


def _ctx(caller_persona: str | None) -> GuardrailContext:
    return GuardrailContext(
        persona_name=caller_persona or "operator:richard",
        task_id="t-1",
        phase="router",
        caller_persona=caller_persona,
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
    assert v.type is VerdictType.BLOCK
    assert v.reason is not None
    assert "mason" in v.reason and "librarian" in v.reason
    assert "may_delegate_to" in v.reason, "the refusal must name the remedy"


@pytest.mark.asyncio
async def test_a_room_with_no_declared_edges_permits_no_delegation() -> None:
    """The load-bearing half of the borrowed pattern. A permissive default here
    would make this a suggestion rather than an authorization control."""
    g = DelegationEdgeGuardrail(edges={})
    v = await g.check(_task("archivist"), _ctx("mason"))
    assert v.type is VerdictType.BLOCK
    assert v.reason is not None and "nothing" in v.reason


@pytest.mark.asyncio
async def test_the_operator_is_not_subject_to_edges() -> None:
    """Edges govern agent-to-agent delegation; a human at the CLI is not
    delegating. `caller_persona is None` means operator BY CONSTRUCTION --
    routes/auth.py branches on `persona_name is None` and that is the only mint
    site producing a token with no cabal_persona claim."""
    g = DelegationEdgeGuardrail(edges={})
    assert (await g.check(_task("archivist"), _ctx(None))).type is VerdictType.PASS


@pytest.mark.asyncio
async def test_an_unidentifiable_target_is_refused_not_waved_through() -> None:
    """A default-deny control that fails open on a missing field is not one."""
    g = DelegationEdgeGuardrail(edges={"mason": ["archivist"]})
    v = await g.check(_task(target_agent=None), _ctx("mason"))
    assert v.type is VerdictType.BLOCK
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
    assert (await g.check(task, _ctx("mason"))).type is VerdictType.BLOCK
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
