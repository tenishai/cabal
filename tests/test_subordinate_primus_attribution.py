"""A subordinate is attributed to the primus that owns it.

Regression: `Subordinate` hard-coded `name=f"rook-{role}"`, and every entry in
`SUBORDINATE_PROMPTS` opened with "You are Rook's ... subordinate". Both were
literals, so a *second* primus (jackdaw) built subordinates that registered as
`rook-implementer` and were told in their system prompt that they worked for
Rook — wrong agent names in bulletin rows / events / traces, and a wrong prompt.

These tests fail against that version: `name` was `rook-implementer` regardless
of owner, and the prompt said "Rook's" regardless of owner.
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


@pytest.mark.parametrize("primus", ["rook", "jackdaw", "mason", "wright"])
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_subordinate_name_is_prefixed_by_its_owning_primus(primus: str, role: str) -> None:
    sub = Subordinate(primus=primus, role=role, llm_client=_client(), model="m")
    assert sub.name == f"{primus}-{role}"


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_canonical_prompt_names_the_owning_primus_not_rook(role: str) -> None:
    """The bug lived in the canonical prompts, not only the fallback — so a
    declared role (implementer/reviewer) is the case that must be pinned."""
    sub = Subordinate(primus="jackdaw", role=role, llm_client=_client(), model="m")
    assert "Jackdaw's" in sub.system_prompt
    assert "Rook" not in sub.system_prompt


def test_unknown_role_falls_back_without_naming_another_primus() -> None:
    sub = Subordinate(primus="wright", role="archaeologist", llm_client=_client(), model="m")
    assert sub.name == "wright-archaeologist"
    assert "Wright's" in sub.system_prompt
    assert "archaeologist" in sub.system_prompt
    assert "Rook" not in sub.system_prompt


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
    never passed an owner, so with TWO primus personas declared, jackdaw's
    subordinates were still built as `rook-implementer`. Unit-testing
    `Subordinate` alone would not have caught that — the defect was the call
    site omitting the owner, not the class mishandling it.
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
            "rook": PersonaConfig(role="primus", subordinates={"implementer": sub}),
            "jackdaw": PersonaConfig(role="primus", subordinates={"implementer": sub}),
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
    assert set(by_name) == {"rook-primus", "jackdaw-primus"}, by_name.keys()

    assert by_name["rook-primus"].subordinates["implementer"].name == "rook-implementer"
    # The actual regression: this was "rook-implementer" before the fix.
    assert by_name["jackdaw-primus"].subordinates["implementer"].name == "jackdaw-implementer"
    assert "Jackdaw's" in by_name["jackdaw-primus"].subordinates["implementer"].system_prompt


def test_prompt_templates_carry_no_hardcoded_primus_name() -> None:
    """Guards the class of bug rather than one instance: a future prompt edit
    that bakes in a name would pass the tests above for that name and fail here."""
    for role, template in SUBORDINATE_PROMPTS.items():
        assert "{primus}" in template, f"{role} prompt must address the primus by placeholder"
        for literal in ("Rook", "Jackdaw", "Mason", "Wright"):
            assert literal not in template, f"{role} prompt hard-codes {literal!r}"


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
