"""Every system prompt carries the shared honesty floor (cabal#138).

Before this, whether an agent was told not to invent depended on who wrote its
persona prompt; only the Archivist had such a rule. The floor is appended where
system prompts are assembled, so these tests look at what actually reaches the
LLM on every hop rather than at any persona's prompt constant.
"""

from __future__ import annotations

import pytest

from cabal.agents.llm_persona import HONESTY_FLOOR
from cabal.agents.personas import PERSONA_REGISTRY
from cabal.agents.personas.librarian import LibrarianPersona
from cabal.agents.personas.wright import WrightPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _task() -> Task:
    return Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="what do you remember?")]),
        ),
    )


def _capturing_client(calls: list[dict]) -> LiteLLMClient:
    async def capture(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    return LiteLLMClient(acompletion_fn=capture, hook_registry=HookRegistry())


def test_floor_states_the_three_rules() -> None:
    text = HONESTY_FLOOR.lower()
    assert "not knowing is an acceptable answer" in text
    assert "capability you cannot demonstrate" in text
    assert "unverified" in text


@pytest.mark.parametrize(
    "name",
    [n for n, cls in PERSONA_REGISTRY.items() if not hasattr(cls, "_aggregate")],
)
def test_every_singleton_persona_gets_it(name: str) -> None:
    persona = PERSONA_REGISTRY[name](_capturing_client([]), model="m")
    system = persona.build_messages(_task())[0]["content"]
    assert HONESTY_FLOOR in system


def test_operator_override_still_gets_it() -> None:
    """`system_prompt_override` replaces the persona prompt, not the floor."""
    persona = LibrarianPersona(_capturing_client([]), model="m")
    persona.system_prompt = "Operator's own voice."
    system = persona.build_messages(_task())[0]["content"]
    assert system.startswith("Operator's own voice.")
    assert HONESTY_FLOOR in system


@pytest.mark.asyncio
async def test_every_primus_hop_gets_it() -> None:
    """PLAN and AGGREGATE build their own messages instead of calling
    build_messages, so they are covered separately from the subordinate leg."""
    calls: list[dict] = []
    client = _capturing_client(calls)
    implementer = Subordinate("wright", "implementer", client, model="m")
    wright = WrightPrimusPersona(client, subordinates={"implementer": implementer}, model="m")

    await wright.handle_task(_task())

    systems = [c["messages"][0]["content"] for c in calls]
    assert len(systems) == 3, systems
    missing = [s.splitlines()[0] for s in systems if HONESTY_FLOOR not in s]
    assert not missing, f"hops without the floor: {missing}"
