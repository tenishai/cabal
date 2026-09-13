"""A subordinate is attributed to the primus that owns it.

Regression: `Subordinate` hard-coded `name=f"rook-{role}"`, and every entry in
`SUBORDINATE_PROMPTS` opened with "You are Rook's ... subordinate". Both were
literals, so a *second* primus (`jackdaw`) built subordinates that registered as
`rook-implementer` and were told in their system prompt that they worked for
Rook — wrong agent names in bulletin rows / events / traces, and a wrong prompt.

These tests fail against that version: `name` was `rook-implementer` regardless
of owner, and the prompt said "Rook's" regardless of owner.

`rook`/`jackdaw` above are the PRE-RENAME primus names and are stated verbatim
on purpose. They were renamed to `mason`/`wright` in the following PR, and the
mechanical rename initially rewrote this paragraph too — turning an accurate
account of a real defect into a claim about names that did not yet exist. A
description of the past has to keep the past's vocabulary.
"""

from __future__ import annotations

from typing import Any

import pytest

from cabal.agents.primus import MAX_LEGS_PER_FANOUT
from cabal.agents.subordinate import SUBORDINATE_PROMPTS, Subordinate
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _client() -> LiteLLMClient:
    async def fn(model: str, messages: list, **kwargs: Any):
        return {"choices": [{"message": {"content": "ok"}}]}

    return LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())


# Four DISTINCT names on purpose, spanning pre- and post-rename vocabulary:
# the point is that any name works, not that these particular ones do. The
# rook->mason rename collapsed this into ["mason","wright","mason","wright"];
# restored deliberately.
@pytest.mark.parametrize("primus", ["rook", "jackdaw", "mason", "wright"])
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_subordinate_name_is_prefixed_by_its_owning_primus(primus: str, role: str) -> None:
    sub = Subordinate(primus=primus, role=role, llm_client=_client(), model="m")
    assert sub.name == f"{primus}-{role}"


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_canonical_prompt_names_the_owning_primus_not_mason(role: str) -> None:
    """The bug lived in the canonical prompts, not only the fallback — so a
    declared role (implementer/reviewer) is the case that must be pinned."""
    sub = Subordinate(primus="wright", role=role, llm_client=_client(), model="m")
    assert "Wright's" in sub.system_prompt
    assert "Mason" not in sub.system_prompt


def test_unknown_role_falls_back_without_naming_another_primus() -> None:
    sub = Subordinate(primus="wright", role="archaeologist", llm_client=_client(), model="m")
    assert sub.name == "wright-archaeologist"
    assert "Wright's" in sub.system_prompt
    assert "archaeologist" in sub.system_prompt
    assert "Mason" not in sub.system_prompt


def test_explicit_system_prompt_still_wins() -> None:
    sub = Subordinate(
        primus="mason",
        role="implementer",
        llm_client=_client(),
        model="m",
        system_prompt="Bespoke.",
    )
    assert sub.system_prompt == "Bespoke."
    assert sub.name == "mason-implementer"


def test_primus_is_required() -> None:
    """A default would reintroduce the bug: every caller would silently inherit
    one primus's identity, which is exactly how the original defect hid."""
    with pytest.raises(TypeError):
        Subordinate(role="implementer", llm_client=_client(), model="m")  # type: ignore[call-arg]


def test_factory_attributes_each_primus_its_own_subordinates() -> None:
    """The integration seam where the bug actually bit.

    `persona_factory` constructs every primus's subordinates. Before the fix it
    never passed an owner, so with TWO primus personas declared, the second
    primus's subordinates were still built under the first primus's name
    (historically: `jackdaw`'s were built as `rook-implementer`). Unit-testing
    `Subordinate` alone would not have caught that — the defect was the call
    site omitting the owner, not the class mishandling it.

    REGISTRY COUPLING: this deliberately names real PERSONA_REGISTRY keys
    (`mason`, `wright`), so a future persona rename will fail it. That is
    correct for an integration test over the registry — it *should* notice the
    registry changing — but the failure is expected, not a regression: update
    the keys and expected names here. (The `rook`→`mason` / `jackdaw`→`wright`
    rename did exactly that, which is why this note exists.)
    """
    from cabal.config.identity import IdentityConfig, PersonaConfig, SubordinateConfig
    from cabal.core.abort import AbortCoordinator
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    from cabal.core.memory.store import NoOpMemoryStore
    from cabal.core.persona_factory import register_personas_from_identity

    registered: list[Any] = []

    class _StubRouter:
        """Only the members the factory touches.

        `input_guardrail_of` arrived with cabal#158: the factory asks the
        router for its delegation-budget instance so a primus SHARES it rather
        than constructing a second one. A stub returning None is the
        budget-disabled case, which is what these tests want.
        """

        abort_coordinator = AbortCoordinator()

        def input_guardrail_of(self, kind: type) -> Any:
            return None

        def register_local(self, handler: Any) -> None:
            registered.append(handler)

        def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
            pass

    sub = SubordinateConfig(model="m")
    identity = IdentityConfig(
        personas={
            "mason": PersonaConfig(role="primus", subordinates={"implementer": sub}),
            "wright": PersonaConfig(role="primus", subordinates={"implementer": sub}),
        }
    )

    register_personas_from_identity(
        dispatch_router=_StubRouter(),  # type: ignore[arg-type]
        identity=identity,
        litellm_client=_client(),
        memory_store=NoOpMemoryStore(),
        session_tracker=NoOpSessionTracker(),
        hook_registry=HookRegistry(),
    )

    by_name = {h.name: h for h in registered}
    assert set(by_name) == {"mason-primus", "wright-primus"}, by_name.keys()

    assert by_name["mason-primus"].subordinates["implementer"].name == "mason-implementer"
    # The actual regression: the second primus's subordinate carried the FIRST
    # primus's name before the fix (then: "rook-implementer" for jackdaw).
    assert by_name["wright-primus"].subordinates["implementer"].name == "wright-implementer"
    assert "Wright's" in by_name["wright-primus"].subordinates["implementer"].system_prompt


def test_persona_wanting_mcp_but_getting_none_says_so() -> None:
    """A persona that opted into MCP and got no tool surface must log why.

    `_Open` is extra="allow", so an identity.toml still carrying the old
    misspelled `[limenal]` section is absorbed as an unknown key rather than
    rejected: `[liminal].url` falls back to None and every persona comes up
    toolless. Before `personas.mcp_skipped` existed there was no `else` on the
    wiring guard and `personas.mcp_wired` only fired on success, so that
    failure was completely silent — the operator saw healthy startup logs and
    personas with no tools.
    """
    import structlog

    from cabal.config.identity import (
        IdentityConfig,
        PersonaConfig,
        PersonaMCPConfig,
        SubordinateConfig,
    )
    from cabal.core.abort import AbortCoordinator
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    from cabal.core.memory.store import NoOpMemoryStore
    from cabal.core.persona_factory import register_personas_from_identity

    class _StubRouter:
        abort_coordinator = AbortCoordinator()

        def input_guardrail_of(self, kind: type) -> Any:
            return None

        def register_local(self, handler: Any) -> None:
            pass

        def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
            pass

    # Wants tools; no [liminal].url configured — the stale-section scenario.
    identity = IdentityConfig(
        personas={
            "mason": PersonaConfig(
                role="primus",
                subordinates={"implementer": SubordinateConfig(model="m")},
                mcp=PersonaMCPConfig(enabled=True),
            )
        }
    )

    with structlog.testing.capture_logs() as logs:
        register_personas_from_identity(
            dispatch_router=_StubRouter(),  # type: ignore[arg-type]
            identity=identity,
            litellm_client=_client(),
            memory_store=NoOpMemoryStore(),
            session_tracker=NoOpSessionTracker(),
            hook_registry=HookRegistry(),
        )

    skipped = [e for e in logs if e.get("event") == "personas.mcp_skipped"]
    assert skipped, f"expected personas.mcp_skipped; got {[e.get('event') for e in logs]}"
    assert skipped[0]["liminal_url_set"] is False
    assert not [e for e in logs if e.get("event") == "personas.mcp_wired"]


def test_persona_not_wanting_mcp_stays_quiet() -> None:
    """No warning when the persona never asked for tools — silence is correct
    there, and a warning on every tool-less persona would train operators to
    ignore the one that matters."""
    import structlog

    from cabal.config.identity import IdentityConfig, PersonaConfig, SubordinateConfig
    from cabal.core.abort import AbortCoordinator
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    from cabal.core.memory.store import NoOpMemoryStore
    from cabal.core.persona_factory import register_personas_from_identity

    class _StubRouter:
        abort_coordinator = AbortCoordinator()

        def input_guardrail_of(self, kind: type) -> Any:
            return None

        def register_local(self, handler: Any) -> None:
            pass

        def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
            pass

    identity = IdentityConfig(
        personas={
            "mason": PersonaConfig(
                role="primus", subordinates={"implementer": SubordinateConfig(model="m")}
            )
        }
    )

    with structlog.testing.capture_logs() as logs:
        register_personas_from_identity(
            dispatch_router=_StubRouter(),  # type: ignore[arg-type]
            identity=identity,
            litellm_client=_client(),
            memory_store=NoOpMemoryStore(),
            session_tracker=NoOpSessionTracker(),
            hook_registry=HookRegistry(),
        )

    assert not [e for e in logs if e.get("event") == "personas.mcp_skipped"]


def test_prompt_templates_carry_no_hardcoded_primus_name() -> None:
    """Guards the class of bug rather than one instance: a future prompt edit
    that bakes in a name would pass the tests above for that name and fail here."""
    for role, template in SUBORDINATE_PROMPTS.items():
        assert "{primus}" in template, f"{role} prompt must address the primus by placeholder"
        # Includes the RETIRED names on purpose: this guard exists to catch a
        # prompt that bakes in any primus name, and a reintroduced "Rook" is
        # exactly as broken as a baked-in "Mason". The rename collapsed these
        # four into two duplicates and silently dropped that half of the guard.
        #
        # CASE-INSENSITIVE deliberately. The capitalised form is how _display()
        # renders a name into prose, but `{primus}-{role}` is the convention
        # everywhere else in this subsystem, so a bake-in is at least as likely
        # to read "report to mason-primus" as "Mason's". Prudence demonstrated
        # on PR #113 that the capitalised-only guard passed on exactly that.
        low = template.lower()
        for literal in ("rook", "jackdaw", "mason", "wright"):
            assert literal not in low, f"{role} prompt hard-codes {literal!r}"


def test_prompt_templates_are_actually_formattable() -> None:
    """Because the prompts are str.format templates, an UNESCAPED literal brace
    — e.g. a JSON example like {"status": "done"} — raises KeyError at
    Subordinate construction. Neither factory call site (app.py:374,
    admin.py:610) wraps `register_personas_from_identity`, so that is a daemon
    that will not boot.

    The comment on SUBORDINATE_PROMPTS says to double literal braces; this
    enforces it. Caught by Prudence on PR #112: the sibling test above passes
    on such a prompt, since it only checks for `{primus}` and absent names —
    so the guard it advertises had a hole exactly where it mattered.
    """
    for role, template in SUBORDINATE_PROMPTS.items():
        try:
            template.format(primus="X", role="y")
        except (KeyError, IndexError, ValueError) as exc:  # pragma: no cover - failure path
            raise AssertionError(
                f"{role} prompt is not formattable ({type(exc).__name__}: {exc}). "
                "Double any literal brace: {{ }}."
            ) from exc


def test_the_factory_hands_a_primus_the_routers_own_budget_instance() -> None:
    """cabal#158 — SHARED, not merely present, and that distinction is the fix.

    The delegation budget is per-conversation, keyed on context_id. A primus
    holding its OWN instance would give one conversation two separate
    allowances: an agent mixing router-mediated hops with fan-out legs would
    get double the budget while looking bounded, which is worse than the
    original gap because it reads as fixed.

    Asserts object IDENTITY against the router's instance. An equality or
    truthiness check would pass against a freshly-constructed guard, which is
    precisely the bug — and a mutation replacing the lookup with
    `DelegationBudgetGuardrail()` survived the suite until this test existed.
    """
    from cabal.config.identity import IdentityConfig, PersonaConfig, SubordinateConfig
    from cabal.core.abort import AbortCoordinator
    from cabal.core.guardrails_builtin import DelegationBudgetGuardrail
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    from cabal.core.memory.store import NoOpMemoryStore
    from cabal.core.persona_factory import register_personas_from_identity

    the_routers_budget = DelegationBudgetGuardrail(limit=7)
    registered: list[Any] = []

    class _StubRouter:
        abort_coordinator = AbortCoordinator()

        def input_guardrail_of(self, kind: type) -> Any:
            return the_routers_budget if kind is DelegationBudgetGuardrail else None

        def register_local(self, handler: Any) -> None:
            registered.append(handler)

        def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
            pass

    identity = IdentityConfig(
        personas={
            "mason": PersonaConfig(
                role="primus", subordinates={"implementer": SubordinateConfig(model="m")}
            )
        }
    )
    register_personas_from_identity(
        dispatch_router=_StubRouter(),  # type: ignore[arg-type]
        identity=identity,
        litellm_client=_client(),
        memory_store=NoOpMemoryStore(),
        session_tracker=NoOpSessionTracker(),
        hook_registry=HookRegistry(),
    )

    primus = {h.name: h for h in registered}["mason-primus"]
    assert primus._delegation_budget is the_routers_budget, (
        "the primus holds a different budget object than the router, so a "
        "conversation gets two allowances instead of one"
    )


def test_every_primus_subclass_forwards_the_budget_it_is_given() -> None:
    """cabal#158 r1 Important 2 — the failure this pins is SILENT.

    Mason's forwarding was held down by three tests; Wright's by nothing.
    Deleting `delegation_budget=delegation_budget` from wright.py left the
    whole suite green — signature still advertising the parameter, factory
    still passing it, mypy and ruff clean, CI green, and `wright-primus`'s
    fan-out unguarded. It matters more for Wright than Mason because
    identity.mtplx.toml configures wright as the deployed primus: the one
    persona actually running was the one whose forwarding nothing held.

    WALKS THE REGISTRY rather than naming Wright, so a subclass added later is
    covered the day it appears. A subclass that does not ACCEPT the kwarg
    fails loudly at startup with a TypeError — the factory passes it
    unconditionally — so accept-and-drop is the only silent shape, and it is
    exactly what this catches.
    """
    from cabal.agents.personas import PERSONA_REGISTRY
    from cabal.agents.primus import PrimusPersona
    from cabal.agents.subordinate import Subordinate
    from cabal.core.guardrails_builtin import DelegationBudgetGuardrail

    sentinel = DelegationBudgetGuardrail(limit=5)
    subclasses = [
        (name, cls)
        for name, cls in PERSONA_REGISTRY.items()
        if isinstance(cls, type) and issubclass(cls, PrimusPersona)
    ]
    assert subclasses, "no primus subclasses found — the walk is testing nothing"

    for name, cls in subclasses:
        sub = Subordinate(
            primus=name, role="implementer", llm_client=_client(), model="m"
        )
        primus = cls(
            _client(),
            subordinates={"implementer": sub},
            delegation_budget=sentinel,
        )
        assert primus._delegation_budget is sentinel, (
            f"{name} accepted delegation_budget and dropped it: its fan-out is "
            "unbounded while the signature and the factory both say otherwise"
        )


def test_the_factory_hands_the_primus_the_ROUTER_S_guard_instances() -> None:
    """THE WIRING, asserted separately from the mechanism (cabal#171).

    Every primus test above builds its own persona and hands it a guard, which
    proves the guard works and says nothing about production. A primus that
    enforces depth in a unit test and silently receives `None` from
    `register_personas_from_identity` would pass all of them while leaving the
    hole cabal#171 was filed for — the counter and the check never meeting.

    Mutating `persona_factory` to stop passing the router's instance survived
    the entire suite until this existed. It is an IDENTITY assertion, not a
    type one: #158 and #171 both exist so one instance is shared rather than a
    second constructed, and a fresh instance of the right class would satisfy
    `isinstance` while counting separately — which is exactly the bug.
    """
    from cabal.config.identity import IdentityConfig, PersonaConfig, SubordinateConfig
    from cabal.core.abort import AbortCoordinator
    from cabal.core.guardrails_builtin import (
        DelegationBudgetGuardrail,
        DelegationDepthGuardrail,
    )
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    from cabal.core.memory.store import NoOpMemoryStore
    from cabal.core.persona_factory import register_personas_from_identity

    registered: list[Any] = []
    router_budget = DelegationBudgetGuardrail(limit=7)
    router_depth = DelegationDepthGuardrail(max_depth=5)
    asked: list[type] = []

    class _StubRouter:
        abort_coordinator = AbortCoordinator()

        def input_guardrail_of(self, kind: type) -> Any:
            asked.append(kind)
            if kind is DelegationBudgetGuardrail:
                return router_budget
            if kind is DelegationDepthGuardrail:
                return router_depth
            return None

        def register_local(self, handler: Any) -> None:
            registered.append(handler)

        def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
            pass

    sub = SubordinateConfig(model="m")
    identity = IdentityConfig(
        personas={
            "mason": PersonaConfig(role="primus", subordinates={"implementer": sub})
        }
    )
    register_personas_from_identity(
        dispatch_router=_StubRouter(),  # type: ignore[arg-type]
        identity=identity,
        litellm_client=_client(),
        memory_store=NoOpMemoryStore(),
        session_tracker=NoOpSessionTracker(),
        hook_registry=HookRegistry(),
    )

    assert DelegationDepthGuardrail in asked, (
        "the factory never asked the router for its depth guard, so a fan-out "
        "leg is not depth-bounded in production"
    )
    assert DelegationBudgetGuardrail in asked

    primus = next(h for h in registered if h.name == "mason-primus")
    assert primus._delegation_depth is router_depth, (
        "the primus did not get the ROUTER'S depth instance — a separate one "
        "counts separately, which is the bug #158 and #171 both exist to stop"
    )
    assert primus._delegation_budget is router_budget


def _subordinate_for_breadth():
    from cabal.agents.subordinate import Subordinate

    return Subordinate(
        primus="mason",
        role="implementer",
        llm_client=_client(),
        model="anthropic/claude-sonnet-4-6",
    )


def test_an_operator_can_LOWER_the_breadth_ceiling_per_persona() -> None:
    """cabal#172 — the config path that made "a policy may only lower it" true.

    Until `max_legs_override` existed, `max_legs` occurred in four files and
    none of them was config: `_max_legs` was `MAX_LEGS_PER_FANOUT` in every
    shipped deployment and only tests could lower it. The clamp was real and
    the story about it was vacuous — accurate about the code, and about a
    policy that had no way to exist.

    This asserts the path end to end, through the real factory, because the
    clamp itself was never the part in doubt.
    """
    from cabal.config.identity import IdentityConfig, PersonaConfig, SubordinateConfig
    from cabal.core.abort import AbortCoordinator
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    from cabal.core.memory.store import NoOpMemoryStore
    from cabal.core.persona_factory import register_personas_from_identity

    registered: list[Any] = []

    class _StubRouter:
        abort_coordinator = AbortCoordinator()

        def input_guardrail_of(self, kind: type) -> Any:
            return None

        def register_local(self, handler: Any) -> None:
            registered.append(handler)

        def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
            pass

    sub = SubordinateConfig(model="m")
    # THREE DECLARED LEGS, CAPPED AT THREE, AND THE COUNTS MUST DIFFER FROM ONE
    # ANOTHER (#178 r1). The first version of this test gave mason ONE leg and
    # an override of 1 -- the single pair of values at which a declaration cap
    # and a runtime throttle are indistinguishable, so it would have passed
    # against either meaning and pinned neither. Three declared against a cap
    # of three is the smallest arrangement where the field is doing its real
    # job: admitting a persona that sits exactly at its policy.
    identity = IdentityConfig(
        personas={
            "mason": PersonaConfig(
                role="primus",
                subordinates={"implementer": sub, "reviewer2": sub, "auditor": sub},
                max_legs_override=3,
            ),
            # No override: must land on the module default, not on mason's 3.
            "wright": PersonaConfig(
                role="primus", subordinates={"implementer": sub}
            ),
        }
    )
    register_personas_from_identity(
        dispatch_router=_StubRouter(),  # type: ignore[arg-type]
        identity=identity,
        litellm_client=_client(),
        memory_store=NoOpMemoryStore(),
        session_tracker=NoOpSessionTracker(),
        hook_registry=HookRegistry(),
    )

    by_name = {h.name: h for h in registered}
    assert by_name["mason-primus"]._max_legs == 3, (
        "the operator's max_legs_override did not reach the primus — the "
        "breadth ceiling is still not settable"
    )
    # AND THE PERSONA ACTUALLY BOOTS WITH ALL THREE. A cap equal to the
    # declared count admits; nothing is trimmed on the way in.
    assert len([r for r in by_name["mason-primus"].subordinates if r != "reviewer"]) == 3
    assert by_name["wright-primus"]._max_legs == MAX_LEGS_PER_FANOUT, (
        "a persona with no override did not get the module default; an "
        "override leaked across personas"
    )


def test_a_cap_below_the_declared_legs_REMOVES_the_persona_it_does_not_narrow_it() -> None:
    """cabal#178 r1 — the field is a declaration cap, so under-setting it is fatal.

    `max_legs_override` is checked ONCE, in the constructor, against
    `subordinates` — which is assigned at construction and never mutated. It
    does not select a subset of legs at dispatch, and the dispatch check cannot
    reach a primus built through the constructor, because `leg_roles` there is
    derived from the same `subordinates` the constructor already bounded.

    So a cap below a persona's declared leg count does not give you a narrower
    persona. It gives you no persona: the constructor raises, `persona_factory`
    catches it, logs `personas.primus_config_rejected`, and the deployment
    boots without that agent.

    That is the shape cabal#172 asked for — a config path feeding the
    constructor check rather than the dispatch one, so a misconfiguration is
    refused at boot instead of failing every dispatch forever after paying for
    a plan call. This test exists because nothing else here distinguishes that
    from a throttle, and the PR that added the field originally described it as
    one.
    """
    from cabal.config.identity import IdentityConfig, PersonaConfig, SubordinateConfig
    from cabal.core.abort import AbortCoordinator
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    from cabal.core.memory.store import NoOpMemoryStore
    from cabal.core.persona_factory import register_personas_from_identity

    registered: list[Any] = []

    class _StubRouter:
        abort_coordinator = AbortCoordinator()

        def input_guardrail_of(self, kind: type) -> Any:
            return None

        def register_local(self, handler: Any) -> None:
            registered.append(handler)

        def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
            pass

    sub = SubordinateConfig(model="m")
    identity = IdentityConfig(
        personas={
            "mason": PersonaConfig(
                role="primus",
                subordinates={"implementer": sub, "reviewer2": sub, "auditor": sub},
                max_legs_override=2,  # BELOW the three it declares
            ),
        }
    )
    register_personas_from_identity(
        dispatch_router=_StubRouter(),  # type: ignore[arg-type]
        identity=identity,
        litellm_client=_client(),
        memory_store=NoOpMemoryStore(),
        session_tracker=NoOpSessionTracker(),
        hook_registry=HookRegistry(),
    )

    assert not [h for h in registered if h.name == "mason-primus"], (
        "a max_legs_override below the declared leg count registered a primus "
        "anyway — the field would then be trimming declared work silently, "
        "which is the one thing the constructor's raise exists to refuse"
    )


def test_an_operator_cannot_RAISE_the_breadth_ceiling_and_is_told() -> None:
    """The clamp holds against config, and no longer does it silently.

    Silently lowering a number an operator wrote down is the same defect shape
    as silently discarding one: the deployment runs bounded, the config says
    otherwise, and nothing reconciles them. That is the failure this repo has
    been paying for elsewhere today, so the clamp announces itself.
    """
    from structlog.testing import capture_logs

    from cabal.agents.personas.mason import MasonPrimusPersona

    # `capture_logs`, NOT pytest's `caplog`. cabal logs through structlog's own
    # processor chain, which never reaches the stdlib handlers caplog installs
    # — a caplog assertion here goes green against a silent clamp AND red
    # against a loud one, i.e. it measures the wrong thing in both directions.
    # Found by writing it the obvious way first and watching the warning appear
    # in captured stdout while the assertion failed.
    with capture_logs() as logs:
        mason = MasonPrimusPersona(
            _client(),
            subordinates={"implementer": _subordinate_for_breadth()},
            max_legs=MAX_LEGS_PER_FANOUT + 5,
        )

    assert mason._max_legs == MAX_LEGS_PER_FANOUT, "a policy raised the ceiling"
    events = [e for e in logs if e.get("event") == "primus.max_legs_clamped"]
    assert events, (
        "the ceiling was clamped without saying so — an operator reading their "
        f"own config would believe a number the deployment is not using: {logs}"
    )
    assert events[0]["requested"] == MAX_LEGS_PER_FANOUT + 5
    assert events[0]["ceiling"] == MAX_LEGS_PER_FANOUT


def test_a_persona_within_the_ceiling_is_not_warned_about() -> None:
    """The control. Without it the assertion above passes on an implementation
    that warns unconditionally, which would train the reader to ignore it."""
    from structlog.testing import capture_logs

    from cabal.agents.personas.mason import MasonPrimusPersona

    with capture_logs() as logs:
        MasonPrimusPersona(
            _client(),
            subordinates={"implementer": _subordinate_for_breadth()},
            max_legs=2,
        )
    assert not [e for e in logs if e.get("event") == "primus.max_legs_clamped"]


def test_a_sub_one_breadth_override_is_refused_not_clamped_up() -> None:
    """The constructor has a FLOOR (`max(1, ...)`), so a zero would silently
    become a working one-leg fan-out. That is the wrong direction for a number
    an operator wrote: zero legs is not a narrow fan-out they could have meant,
    it is a primus that can never dispatch. Refuse at the config boundary
    rather than let the floor reinterpret it.

    Found because a mutant that neutered this validator survived everything
    else in the suite.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from cabal.config.identity import PersonaConfig

    for bad in (0, -1):
        with _pytest.raises(ValidationError, match="max_legs_override must be >= 1"):
            PersonaConfig(role="primus", max_legs_override=bad)

    # And the floor is still there for a DIRECT constructor caller, which is
    # the path the validator does not guard.
    from cabal.agents.personas.mason import MasonPrimusPersona

    mason = MasonPrimusPersona(
        _client(),
        subordinates={"implementer": _subordinate_for_breadth()},
        max_legs=0,
    )
    assert mason._max_legs == 1
