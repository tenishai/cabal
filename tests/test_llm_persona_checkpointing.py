"""cabal#2 — LLMPersona writes checkpoints around LLM calls + supports resume.

Uses a RecordingCheckpointStore stub so we can assert exact write/drop
sequencing. The kill-then-resume e2e (against a real SqliteCheckpointStore)
exercises the realistic dispatch path the operator hits via the admin
endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.checkpoint import (
    Checkpoint,
    NoOpCheckpointStore,
    SqliteCheckpointStore,
)
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


def _task(task_id: str = "t-ck", text: str = "hello") -> Task:
    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
        metadata={"cabal_trace_id": "tr-1"},
    )


def _llm_stub(reply: str = "the answer"):
    """LLM stub that always returns plain text (no tool_calls)."""

    async def fn(model: str, messages: list, **kwargs):
        return {"choices": [{"message": {"content": reply}}]}

    return fn


@dataclass
class RecordingCheckpointStore:
    """Captures every save/load/drop call for assertions."""

    saves: list[Checkpoint] = field(default_factory=list)
    loads: list[str] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)
    fail_save: bool = False

    async def save(self, checkpoint: Checkpoint) -> None:
        if self.fail_save:
            raise RuntimeError("simulated checkpoint backend failure")
        self.saves.append(checkpoint)

    async def load(self, task_id: str) -> Checkpoint | None:
        self.loads.append(task_id)
        for ckpt in reversed(self.saves):
            if ckpt.task_id == task_id:
                return ckpt
        return None

    async def drop(self, task_id: str) -> bool:
        self.drops.append(task_id)
        return True


# ---- baseline: no-op store doesn't interfere ------------------------------


@pytest.mark.asyncio
async def test_noop_store_default_does_not_block_completion() -> None:
    """The default NoOpCheckpointStore lets handle_task complete normally
    without persisting anything."""
    client = LiteLLMClient(acompletion_fn=_llm_stub("done"), hook_registry=HookRegistry())
    persona = LLMPersona(name="archivist", model="x", llm_client=client)

    result = await persona.handle_task(_task())

    assert result.status.state == "completed"
    assert isinstance(persona.checkpoint_store, NoOpCheckpointStore)


# ---- save/drop sequencing on the happy path -------------------------------


@pytest.mark.asyncio
async def test_handle_task_saves_before_llm_call_and_drops_on_completion() -> None:
    """One checkpoint write (step 0) before the LLM call, then drop on
    successful completion. No tool-use loop, so only the initial save."""
    store = RecordingCheckpointStore()
    client = LiteLLMClient(acompletion_fn=_llm_stub(), hook_registry=HookRegistry())
    persona = LLMPersona(name="architect", model="x", llm_client=client, checkpoint_store=store)

    result = await persona.handle_task(_task("t-1"))

    assert result.status.state == "completed"
    assert len(store.saves) == 1
    assert store.saves[0].task_id == "t-1"
    assert store.saves[0].step_index == 0
    # First message is the system prompt; the user message follows.
    assert store.saves[0].messages[0]["role"] == "system"
    assert any(m["role"] == "user" for m in store.saves[0].messages)
    # task_json captures enough to reconstruct the Task on resume.
    assert store.saves[0].task_json["id"] == "t-1"
    assert store.drops == ["t-1"]


# ---- crash + resume e2e --------------------------------------------------


@pytest.mark.asyncio
async def test_crash_then_resume_completes_from_checkpoint() -> None:
    """Simulate the daemon dying inside the initial LLM call: the
    checkpoint persists. Resume with `resume_from=checkpoint` continues
    the work to completion and drops the checkpoint."""
    store = RecordingCheckpointStore()

    async def kaboom(model: str, messages: list, **kwargs):
        raise RuntimeError("daemon process killed mid-call")

    crashed_client = LiteLLMClient(acompletion_fn=kaboom, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="architect",
        model="x",
        llm_client=crashed_client,
        checkpoint_store=store,
    )

    with pytest.raises(RuntimeError, match="daemon process killed"):
        await persona.handle_task(_task("t-resume"))

    # The save fired BEFORE the crash; drop did NOT.
    assert len(store.saves) == 1
    assert store.saves[0].step_index == 0
    assert store.drops == []

    # The operator restarts: persona now has a working LLM and resume_from
    # the saved checkpoint. Should complete + drop.
    saved = store.saves[0]
    healthy_client = LiteLLMClient(
        acompletion_fn=_llm_stub("recovered answer"), hook_registry=HookRegistry()
    )
    persona.llm_client = healthy_client

    result = await persona.handle_task(_task("t-resume"), resume_from=saved)

    assert result.status.state == "completed"
    assert "recovered answer" in (
        result.status.message.parts[0].text if result.status.message else ""
    )
    assert store.drops == ["t-resume"]


# ---- tool-use loop checkpoints advance step_index -------------------------


@pytest.mark.asyncio
async def test_tool_use_loop_checkpoints_advance_step_index() -> None:
    """Each tool-use iteration writes a fresh checkpoint with the next
    step_index. Verifies the saved messages include the tool output so
    resume picks up after the executed tool, not before it."""
    store = RecordingCheckpointStore()

    # Stub LLM: first response asks for tool_use; second returns text.
    response_seq = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"value": "ping"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "final after tool"}}]},
        ]
    )

    async def fn(model: str, messages: list, **kwargs):
        return next(response_seq)

    class FakeProxy:
        async def list_tools_for_llm(self) -> list[dict[str, Any]]:
            return [{"type": "function", "function": {"name": "echo"}}]

        async def call_tool(self, name: str, arguments: dict) -> Any:
            return f"echoed:{arguments.get('value')}"

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="architect",
        model="x",
        llm_client=client,
        checkpoint_store=store,
        tool_proxy=FakeProxy(),
        tool_use_max_steps=4,
    )

    result = await persona.handle_task(_task("t-tools"))

    assert result.status.state == "completed"
    # Two saves: one at step 0 (initial), one at step 1 (after tool round)
    assert [c.step_index for c in store.saves] == [0, 1]
    # Step-1 checkpoint MUST include the tool result so resume continues
    # after the tool, not before it.
    step1_roles = [m.get("role") for m in store.saves[1].messages]
    assert "tool" in step1_roles
    assert store.drops == ["t-tools"]


# ---- save backend failure does NOT block dispatch ------------------------


@pytest.mark.asyncio
async def test_handle_task_propagates_save_errors() -> None:
    """If the checkpoint backend itself fails, surface that — silently
    swallowing would leave the operator with no signal that durability
    is broken. The Task remains in `submitted` state in the DB; the
    operator can investigate."""
    store = RecordingCheckpointStore(fail_save=True)
    client = LiteLLMClient(acompletion_fn=_llm_stub(), hook_registry=HookRegistry())
    persona = LLMPersona(name="architect", model="x", llm_client=client, checkpoint_store=store)

    with pytest.raises(RuntimeError, match="simulated checkpoint backend failure"):
        await persona.handle_task(_task("t-save-fail"))


# ---- e2e: real SqliteCheckpointStore + kill simulation -------------------


@pytest.mark.asyncio
async def test_kill_then_resume_against_real_sqlite_store(app) -> None:
    """End-to-end with the real SQLite-backed store: write checkpoint
    on a crash, then resume from disk."""
    store = SqliteCheckpointStore(sessionmaker=app.state.sessionmaker)

    async def kaboom(model: str, messages: list, **kwargs):
        raise RuntimeError("crash")

    persona = LLMPersona(
        name="architect",
        model="x",
        llm_client=LiteLLMClient(acompletion_fn=kaboom, hook_registry=HookRegistry()),
        checkpoint_store=store,
    )

    with pytest.raises(RuntimeError, match="crash"):
        await persona.handle_task(_task("t-e2e"))

    # Disk reflects the saved state.
    loaded = await store.load("t-e2e")
    assert loaded is not None
    assert loaded.step_index == 0
    assert loaded.task_json["id"] == "t-e2e"

    # Resume against a healthy LLM.
    persona.llm_client = LiteLLMClient(
        acompletion_fn=_llm_stub("recovered"), hook_registry=HookRegistry()
    )
    result = await persona.handle_task(_task("t-e2e"), resume_from=loaded)

    assert result.status.state == "completed"
    # Checkpoint dropped from disk on completion.
    assert await store.load("t-e2e") is None
