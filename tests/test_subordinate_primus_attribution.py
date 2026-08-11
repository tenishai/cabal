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
        """Only the three members the factory touches."""

        abort_coordinator = AbortCoordinator()

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
