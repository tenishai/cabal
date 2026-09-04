"""Provider kwargs from identity.toml reach every LLM call site.

The motivating case: a reasoning model whose thinking must be switched off
per-call (`chat_template_kwargs={"enable_thinking": False}`). With it, a
question answers in 49 tokens; without it, the model spends its entire budget
thinking and returns empty content. cabal had the field but no way to set it,
and two of the four call paths dropped it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cabal.agents.personas.wright import WrightPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.config.identity import (
    RESERVED_EXTRA_KWARGS,
    PersonaConfig,
    SubordinateConfig,
)
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient

THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}


def _task(text: str = "do the thing") -> Task:
    return Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _capturing_client(calls: list[dict]) -> LiteLLMClient:
    async def capture(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    return LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())


def test_config_accepts_arbitrary_provider_kwargs() -> None:
    """Untyped by design — the provider owns the key set, not cabal."""
    cfg = PersonaConfig(role="singleton", extra_kwargs=THINKING_OFF)
    assert cfg.extra_kwargs == THINKING_OFF
    assert PersonaConfig(role="singleton").extra_kwargs == {}


def test_subordinate_kwargs_default_to_inheriting() -> None:
    """None means inherit; an explicit {} means send nothing."""
    assert SubordinateConfig(model="m").extra_kwargs is None
    assert SubordinateConfig(model="m", extra_kwargs={}).extra_kwargs == {}


@pytest.mark.asyncio
async def test_a_plain_persona_forwards_them() -> None:
    calls: list[dict] = []
    sub = Subordinate(
        "wright", "implementer", _capturing_client(calls), model="m",
    )
    sub.extra_kwargs = dict(THINKING_OFF)
    await sub.handle_task(_task())

    assert calls, "expected an LLM call"
    assert calls[0]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_both_primus_stages_forward_them() -> None:
    """THE REGRESSION THIS FILE EXISTS FOR.

    `extra_kwargs` reached LLMPersona's own call and its stream, but `_plan`
    and `_aggregate` built their acompletion calls by hand and omitted it — so
    a knob set on a primus applied to its subordinates and not to the primus
    itself, on the two hops an operator would notice first.
    """
    calls: list[dict] = []
    client = _capturing_client(calls)
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(
        client, subordinates={"implementer": implementer}, model="m",
    )
    wright.extra_kwargs = dict(THINKING_OFF)
    implementer.extra_kwargs = dict(THINKING_OFF)

    await wright.handle_task(_task())

    # `agent=` is consumed by the client for logging and never reaches the
    # provider call, so the hops are identified by their system prompts.
    systems = [c["messages"][0]["content"] for c in calls]
    assert len(calls) == 3, systems
    assert any("your job is to PLAN" in s for s in systems), "no plan hop"
    assert any("implementer subordinate" in s for s in systems), "no subordinate hop"
    assert any("implementer has returned output" in s for s in systems), "no aggregate hop"

    dropped = [
        s.splitlines()[0]
        for s, c in zip(systems, calls, strict=True)
        if c.get("chat_template_kwargs") != {"enable_thinking": False}
    ]
    assert not dropped, f"these call sites dropped the kwargs: {dropped}"


@pytest.mark.asyncio
async def test_absent_config_sends_nothing_extra() -> None:
    """Guards the guard: the default must not inject keys the provider may
    reject — every model cabal talks to would see them."""
    calls: list[dict] = []
    sub = Subordinate("wright", "implementer", _capturing_client(calls), model="m")
    await sub.handle_task(_task())

    assert calls
    assert "chat_template_kwargs" not in calls[0]


# --- review findings on PR #131 ---------------------------------------------


def test_reserved_keys_are_refused_at_config_load() -> None:
    """FINDING (Important). Collisions with cabal's own kwargs fail badly and
    in two opposite directions, both measured on the live dispatch path:

      max_tokens -> TypeError on EVERY dispatch, hours after a clean boot
      api_base   -> silently WINS over daemon config, 200 OK, no log line

    Both become one boot-time error naming the key.
    """
    for key, value in [
        ("max_tokens", 999),
        ("api_base", "http://evil.invalid/v1"),
        ("api_key", "sk-nope"),
        ("tools", []),
        ("model", "someone/else"),
        ("messages", []),
    ]:
        with pytest.raises(ValidationError, match="cabal binds these itself"):
            PersonaConfig(role="singleton", extra_kwargs={key: value})
        with pytest.raises(ValidationError, match="cabal binds these itself"):
            SubordinateConfig(model="m", extra_kwargs={key: value})


def test_the_error_names_the_offending_key() -> None:
    """A guard the operator cannot act on is only half a guard."""
    with pytest.raises(ValidationError) as exc:
        PersonaConfig(role="singleton", extra_kwargs={"max_tokens": 1, "api_key": "x"})
    msg = str(exc.value)
    assert "'api_key'" in msg and "'max_tokens'" in msg
    assert "max_tokens_override" in msg, "should point at the right field"


def test_provider_keys_still_pass() -> None:
    """Guards the guard: the reserved set must not swallow the actual use case."""
    cfg = PersonaConfig(role="singleton", extra_kwargs=THINKING_OFF)
    assert cfg.extra_kwargs == THINKING_OFF
    assert SubordinateConfig(
        model="m", extra_kwargs={"reasoning_effort": "low"}
    ).extra_kwargs == {"reasoning_effort": "low"}


def test_reserved_covers_the_client_signature() -> None:
    """Pins the set to cabal's OWN signature, which is why it is not a treadmill.

    Every keyword-only parameter LiteLLMClient.acompletion binds by name must be
    reserved; if someone adds one, this fails instead of shipping a key that
    raises TypeError at dispatch time.
    """
    import inspect

    from cabal.core.llm.litellm_client import LiteLLMClient

    for name, param in inspect.signature(LiteLLMClient.acompletion).parameters.items():
        if param.kind is inspect.Parameter.KEYWORD_ONLY:
            assert name in RESERVED_EXTRA_KWARGS, f"{name!r} is bindable but not reserved"
