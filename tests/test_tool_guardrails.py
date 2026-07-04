"""Tool-boundary guardrails — cabal#52.

Covers:
  - Pre-tool BLOCK: call_tool is never invoked; an error tool message is
    injected into the conversation; dispatch continues and the task completes.
  - Pre-tool REWRITE: call_tool receives the patched name/arguments.
  - Post-tool REWRITE: the result string the LLM sees is replaced.
  - Post-tool BLOCK: a policy-error string is injected in place of the result.
  - Empty lists: existing behavior is completely unchanged.

Stubs mirror test_activity_logger.py (_FakeToolProxy / _tool_call_stub).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.guardrails import GuardrailContext, Verdict
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient

# ---------------------------------------------------------------------------
# Stubs — mirror test_activity_logger.py patterns
# ---------------------------------------------------------------------------


class _FakeOutcome:
    """Minimal duck-typed ToolCallOutcome (no limenal dependency)."""

    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [{"type": "text", "text": text}]
        self.is_error = is_error


class _FakeToolProxy:
    """Records every call_tool invocation; returns a canned outcome."""

    def __init__(self, result_text: str = "42", *, is_error: bool = False) -> None:
        self._result = _FakeOutcome(result_text, is_error=is_error)
        self.calls: list[dict[str, Any]] = []

    async def list_tools_for_llm(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "calculator__add",
                    "description": "Add two numbers",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeOutcome:
        self.calls.append({"name": name, "arguments": arguments})
        return self._result


def _tool_call_stub(
    tool_name: str = "calculator__add",
    arguments: dict[str, Any] | None = None,
    final_reply: str = "done",
):
    """LLM stub: first call returns one tool_call; subsequent calls return text."""
    if arguments is None:
        arguments = {"a": 1, "b": 2}
    state = {"step": 0}
    captured: list[list[dict[str, Any]]] = []

    async def fn(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict:
        captured.append(list(messages))
        state["step"] += 1
        if state["step"] == 1 and "tools" in kwargs:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-001",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": final_reply}}]}

    fn.captured = captured  # type: ignore[attr-defined]
    return fn


def _user_task(task_id: str = "t-tg-1", text: str = "use a tool") -> Task:
    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


# ---------------------------------------------------------------------------
# Example guardrails (test-only)
# ---------------------------------------------------------------------------


class _BlockNamedTool:
    """Block any tool call whose name matches the given string."""

    def __init__(self, tool_name: str, reason: str = "forbidden tool") -> None:
        self.name = f"block_named_tool:{tool_name}"
        self._tool_name = tool_name
        self._reason = reason

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        if isinstance(target, dict) and target.get("name") == self._tool_name:
            return Verdict.block(self._reason)
        return Verdict.pass_()


class _PatchArguments:
    """REWRITE: replace the 'arguments' field in the tool call target."""

    def __init__(self, new_args: dict[str, Any]) -> None:
        self.name = "patch_arguments"
        self._new_args = new_args

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        if isinstance(target, dict) and "arguments" in target:
            patched = {**target, "arguments": self._new_args}
            return Verdict.rewrite(patched)
        return Verdict.pass_()


class _ReplaceResult:
    """REWRITE: replace the tool result string entirely."""

    def __init__(self, replacement: str) -> None:
        self.name = "replace_result"
        self._replacement = replacement

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        if isinstance(target, str):
            return Verdict.rewrite(self._replacement)
        return Verdict.pass_()


class _BlockResult:
    """BLOCK any tool result (post-tool phase)."""

    def __init__(self, reason: str = "result blocked") -> None:
        self.name = "block_result"
        self._reason = reason

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        return Verdict.block(self._reason)


# ---------------------------------------------------------------------------
# 1. Pre-tool BLOCK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_tool_block_skips_call_tool_and_injects_error_message() -> None:
    """A tool_call_guardrail that blocks a named tool must:
    - never call call_tool (proxy.calls stays empty)
    - inject an error tool message into the conversation
    - allow dispatch to continue and complete (task is NOT failed)
    """
    stub = _tool_call_stub(tool_name="calculator__add", final_reply="I couldn't add those.")
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="should never appear")

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_call_guardrails=[_BlockNamedTool("calculator__add", "addition is forbidden")],
    )

    result = await persona.handle_task(_user_task())

    # call_tool must not have been invoked.
    assert proxy.calls == [], f"call_tool was called: {proxy.calls}"

    # Task must still complete (BLOCK blocks the call, not the whole task).
    assert result.status.state == "completed", result.status

    # The second LLM call (after the blocked tool) must have seen a tool
    # message with the block reason.
    second_call_messages = stub.captured[1]  # type: ignore[attr-defined]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "addition is forbidden" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "call-001"


# ---------------------------------------------------------------------------
# 2. Pre-tool REWRITE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_tool_rewrite_patches_arguments() -> None:
    """A tool_call_guardrail REWRITE must change what call_tool receives."""
    original_args = {"a": 1, "b": 2}
    patched_args = {"a": 99, "b": 1}

    stub = _tool_call_stub(tool_name="calculator__add", arguments=original_args)
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="100")

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_call_guardrails=[_PatchArguments(patched_args)],
    )

    result = await persona.handle_task(_user_task())

    assert result.status.state == "completed"
    # call_tool must have been called once with the patched args.
    assert len(proxy.calls) == 1
    assert proxy.calls[0]["arguments"] == patched_args, (
        f"expected {patched_args}, got {proxy.calls[0]['arguments']}"
    )


# ---------------------------------------------------------------------------
# 3. Post-tool REWRITE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_tool_rewrite_replaces_result_content() -> None:
    """A tool_result_guardrail REWRITE must replace the result the LLM sees."""
    stub = _tool_call_stub(tool_name="calculator__add")
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="42")  # real outcome

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_result_guardrails=[_ReplaceResult("REDACTED")],
    )

    result = await persona.handle_task(_user_task())

    assert result.status.state == "completed"
    # call_tool was still invoked.
    assert len(proxy.calls) == 1
    # The second LLM call must have seen the rewritten result, not "42".
    second_call_messages = stub.captured[1]  # type: ignore[attr-defined]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "REDACTED", (
        f"expected 'REDACTED', got {tool_msgs[0]['content']!r}"
    )


# ---------------------------------------------------------------------------
# 4. Post-tool BLOCK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_tool_block_injects_policy_error_in_result() -> None:
    """A tool_result_guardrail BLOCK must replace the result with a policy-error
    string; call_tool IS still called (the block is post-call)."""
    stub = _tool_call_stub(tool_name="calculator__add")
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="42")

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_result_guardrails=[_BlockResult("result contains sensitive data")],
    )

    result = await persona.handle_task(_user_task())

    assert result.status.state == "completed"
    # call_tool was called (block is post-call).
    assert len(proxy.calls) == 1
    # The second LLM call must have seen the policy-error string, not "42".
    second_call_messages = stub.captured[1]  # type: ignore[attr-defined]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "result contains sensitive data" in tool_msgs[0]["content"]
    assert "42" not in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 5. Empty lists = existing behavior unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_tool_guardrail_lists_unchanged() -> None:
    """No tool guardrails (default) must produce exactly the same behavior as
    before cabal#52: call_tool is called with original args, result is passed
    through unmodified, task completes."""
    original_args = {"a": 1, "b": 2}
    stub = _tool_call_stub(tool_name="calculator__add", arguments=original_args)
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="3")

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        # Both lists default to [] — no guardrails.
    )

    result = await persona.handle_task(_user_task())

    assert result.status.state == "completed"
    assert len(proxy.calls) == 1
    assert proxy.calls[0]["name"] == "calculator__add"
    assert proxy.calls[0]["arguments"] == original_args
    # LLM saw the real result, not a rewrite.
    second_call_messages = stub.captured[1]  # type: ignore[attr-defined]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "3"


# ---------------------------------------------------------------------------
# 6. Multiple tool calls in one round — BLOCK on one, other proceeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_tool_block_on_one_of_multiple_tool_calls() -> None:
    """When a round has two tool calls, blocking one must not prevent the other
    from executing. Both tool result messages must appear in the next turn."""
    state = {"step": 0}
    captured: list[list[dict[str, Any]]] = []

    async def _two_tool_stub(
        model: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> dict:
        captured.append(list(messages))
        state["step"] += 1
        if state["step"] == 1 and "tools" in kwargs:
            # Return two tool calls: one blocked, one allowed.
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-A",
                                    "type": "function",
                                    "function": {
                                        "name": "forbidden__tool",
                                        "arguments": json.dumps({}),
                                    },
                                },
                                {
                                    "id": "call-B",
                                    "type": "function",
                                    "function": {
                                        "name": "allowed__tool",
                                        "arguments": json.dumps({"x": 7}),
                                    },
                                },
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "both handled"}}]}

    llm_client = LiteLLMClient(acompletion_fn=_two_tool_stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="allowed_result")

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_call_guardrails=[_BlockNamedTool("forbidden__tool", "not allowed")],
    )

    result = await persona.handle_task(_user_task())

    assert result.status.state == "completed"
    # Only the allowed tool was dispatched.
    assert len(proxy.calls) == 1
    assert proxy.calls[0]["name"] == "allowed__tool"

    # Both tool result messages must appear in the second LLM call.
    second_messages = captured[1]
    tool_msgs = [m for m in second_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2

    # Find by tool_call_id.
    by_id = {m["tool_call_id"]: m for m in tool_msgs}
    assert "call-A" in by_id
    assert "call-B" in by_id
    assert "not allowed" in by_id["call-A"]["content"]
    assert by_id["call-B"]["content"] == "allowed_result"


# ---------------------------------------------------------------------------
# 7. GuardrailContext phase values are correct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_guardrail_context_phase_values() -> None:
    """Guardrails receive the expected phase strings: 'tool_call' and 'tool_result'."""
    pre_phases: list[str] = []
    post_phases: list[str] = []

    class _RecordPrePhase:
        name = "record_pre_phase"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            pre_phases.append(ctx.phase)
            return Verdict.pass_()

    class _RecordPostPhase:
        name = "record_post_phase"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            post_phases.append(ctx.phase)
            return Verdict.pass_()

    stub = _tool_call_stub()
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy()

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_call_guardrails=[_RecordPrePhase()],
        tool_result_guardrails=[_RecordPostPhase()],
    )

    result = await persona.handle_task(_user_task())
    assert result.status.state == "completed"

    assert pre_phases == ["tool_call"]
    assert post_phases == ["tool_result"]


# ---------------------------------------------------------------------------
# 6. Audit-trail fidelity when a guardrail acts (Prudence #57: #1/#2/#3)
# ---------------------------------------------------------------------------


def _capture_tool_use() -> tuple[HookRegistry, list[dict[str, Any]]]:
    """A HookRegistry with a TOOL_USE handler that records every payload."""
    registry = HookRegistry()
    captured: list[dict[str, Any]] = []

    async def _capture(payload: dict[str, Any]) -> None:
        captured.append(payload)

    registry.register(HookName.TOOL_USE, _capture, label="capture")
    return registry, captured


@pytest.mark.asyncio
async def test_pre_tool_block_records_error_row() -> None:
    """A pre-tool BLOCK fires exactly one TOOL_USE row with is_error=True and the
    block reason as the result — a policy denial is visible in the audit log
    (Prudence #57 Important #1/#2)."""
    registry, captured = _capture_tool_use()
    llm_client = LiteLLMClient(
        acompletion_fn=_tool_call_stub(final_reply="ok"), hook_registry=registry
    )
    proxy = _FakeToolProxy(result_text="never")
    persona = LLMPersona(
        name="t",
        model="m",
        llm_client=llm_client,
        tool_proxy=proxy,
        tool_call_guardrails=[_BlockNamedTool("calculator__add", reason="nope")],
    )
    result = await persona.handle_task(_user_task("t-audit-block"))
    assert result.status.state == "completed"
    assert proxy.calls == []  # never dispatched

    assert len(captured) == 1, captured
    row = captured[0]
    assert row["is_error"] is True
    assert "blocked by policy" in row["result_summary"]
    assert "nope" in row["result_summary"]
    assert row["latency_ms"] == 0


@pytest.mark.asyncio
async def test_post_tool_block_records_error_row() -> None:
    """A post-tool BLOCK marks the TOOL_USE row is_error=True even though the
    tool itself ran cleanly (Prudence #57 Important #2)."""
    registry, captured = _capture_tool_use()
    llm_client = LiteLLMClient(
        acompletion_fn=_tool_call_stub(final_reply="ok"), hook_registry=registry
    )
    proxy = _FakeToolProxy(result_text="clean result")  # not an error
    persona = LLMPersona(
        name="t",
        model="m",
        llm_client=llm_client,
        tool_proxy=proxy,
        tool_result_guardrails=[_BlockResult(reason="policy")],
    )
    result = await persona.handle_task(_user_task("t-audit-postblock"))
    assert result.status.state == "completed"
    assert len(proxy.calls) == 1  # the tool DID run

    assert len(captured) == 1, captured
    row = captured[0]
    assert row["is_error"] is True  # forced True by the block, not stale False
    assert "blocked by policy" in row["result_summary"]


@pytest.mark.asyncio
async def test_malformed_rewrite_blocks_without_crashing() -> None:
    """A guardrail returning a REWRITE dict missing 'arguments' must not KeyError
    and crash the Task — it degrades to a blocked call (Prudence #57 Important
    #3)."""
    registry, captured = _capture_tool_use()

    class _MalformedRewrite:
        name = "malformed_rewrite"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            if isinstance(target, dict):
                return Verdict.rewrite({"name": "calculator__add"})  # no 'arguments'
            return Verdict.pass_()

    llm_client = LiteLLMClient(
        acompletion_fn=_tool_call_stub(final_reply="ok"), hook_registry=registry
    )
    proxy = _FakeToolProxy(result_text="never")
    persona = LLMPersona(
        name="t",
        model="m",
        llm_client=llm_client,
        tool_proxy=proxy,
        tool_call_guardrails=[_MalformedRewrite()],
    )
    result = await persona.handle_task(_user_task("t-malformed"))
    # Task completes (not crashed), the tool is NOT dispatched, and the row
    # records the malformed rewrite as an error.
    assert result.status.state == "completed"
    assert proxy.calls == []
    assert len(captured) == 1, captured
    assert captured[0]["is_error"] is True
    assert "malformed" in captured[0]["result_summary"]
