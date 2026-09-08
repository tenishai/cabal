"""Every system prompt cabal assembles carries the honesty floor (cabal#138).

Operator requirement: "those should be a default for any agents we work with,
we need to know the information we get back isn't fabricated."

Not left to persona authors. Only the Archivist carried an anti-fabrication
rule, and it did not stop that persona from claiming a memory store it did not
have, or from citing PRs absent from the store it did have.
"""

from __future__ import annotations

import pytest

from cabal.agents.llm_persona import HONESTY_FLOOR
from cabal.agents.personas.archivist import ArchivistPersona
from cabal.agents.personas.wright import WrightPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _task(tid: str = "t-1") -> Task:
    return Task(
        id=tid,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="what do you know?")]),
        ),
    )


def _capturing_client(systems: list[str]) -> LiteLLMClient:
    async def fn(**kwargs):
        systems.append(
            next((m["content"] for m in kwargs["messages"] if m["role"] == "system"), "")
        )
        return {"choices": [{"message": {"content": "ok"}}, ]}

    return LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())


def test_the_floor_says_the_three_things_it_must() -> None:
    """The wording may be tuned; these three properties may not be dropped."""
    floor = HONESTY_FLOOR.lower()
    assert "i don't know" in floor, "not-knowing must be named as acceptable"
    assert "capability" in floor, "capability claims are how the Archivist failed"
    assert "unverified" in floor or "retrieved" in floor, "provenance must be required"


@pytest.mark.asyncio
async def test_a_plain_persona_carries_it() -> None:
    systems: list[str] = []
    p = Subordinate("wright", "implementer", _capturing_client(systems), model="m")

    await p.handle_task(_task())

    assert systems and HONESTY_FLOOR in systems[0]


@pytest.mark.asyncio
async def test_the_archivist_carries_it() -> None:
    """The persona whose own anti-fabrication rule proved insufficient."""
    systems: list[str] = []
    p = ArchivistPersona(_capturing_client(systems), model="m")

    await p.handle_task(_task())

    assert systems and HONESTY_FLOOR in systems[0]


@pytest.mark.asyncio
async def test_both_primus_hops_carry_it() -> None:
    """The plan and aggregate hops build their own message lists rather than
    going through build_messages, so they are the two sites a floor added in
    one place would silently miss."""
    systems: list[str] = []
    client = _capturing_client(systems)
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(
        client, subordinates={"implementer": implementer}, model="m"
    )

    await wright.handle_task(_task("t-primus"))

    assert len(systems) == 3, [s[:40] for s in systems]
    missing = [s.splitlines()[0] for s in systems if HONESTY_FLOOR not in s]
    assert not missing, f"these hops dropped the floor: {missing}"


@pytest.mark.asyncio
async def test_the_streaming_path_carries_it() -> None:
    """astream_task reuses build_messages, so it inherits the floor — pinned
    because that reuse is an implementation detail someone could undo."""
    systems: list[str] = []

    async def fn(**kwargs):
        systems.append(
            next((m["content"] for m in kwargs["messages"] if m["role"] == "system"), "")
        )

        class _Stream:
            def __aiter__(self):
                async def gen():
                    yield {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}

                return gen()

        return _Stream()

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    p = Subordinate("wright", "implementer", client, model="m")

    async for _ in p.astream_task(_task()):
        pass

    assert systems and HONESTY_FLOOR in systems[0]


@pytest.mark.asyncio
async def test_the_floor_does_not_displace_the_persona_prompt() -> None:
    """Appended, not substituted: a persona's own instructions must survive."""
    systems: list[str] = []
    p = ArchivistPersona(_capturing_client(systems), model="m")

    await p.handle_task(_task())

    assert "Cabal's Archivist" in systems[0], "persona prompt lost"
    assert systems[0].index("Cabal's Archivist") < systems[0].index(HONESTY_FLOOR.strip()[:20])
