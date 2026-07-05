"""ValidateBeforeRecord guardrail — cabal#43.

Covers:
  - Integration: persona dispatches code_edit (mutating) → validator FAILS →
    result is BLOCKED; the tool message the next LLM turn sees contains the
    validation error, and the TOOL_USE hook fires with is_error=True.
  - Non-mutating tools pass through untouched (no validation call).
  - The validator receives the correct tool_name / arguments / result.
  - Unit isolation: check() with a hand-built GuardrailContext.
  - Predicate form of mutating_tools (Callable[[str], bool]).

Stubs mirror test_tool_guardrails.py (_FakeToolProxy / _tool_call_stub).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.guardrails import GuardrailContext
from cabal.core.guardrails_builtin import ValidateBeforeRecord
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient

# ---------------------------------------------------------------------------
# Stubs (mirror test_tool_guardrails.py)
# ---------------------------------------------------------------------------


class _FakeOutcome:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [{"type": "text", "text": text}]
        self.is_error = is_error


class _FakeToolProxy:
    """Records every call_tool invocation; returns a canned outcome."""

    def __init__(self, result_text: str = "ok", *, is_error: bool = False) -> None:
        self._result = _FakeOutcome(result_text, is_error=is_error)
        self.calls: list[dict[str, Any]] = []

    async def list_tools_for_llm(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "code_edit",
                    "description": "Edit source code",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file (non-mutating)",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeOutcome:
        self.calls.append({"name": name, "arguments": arguments})
        return self._result


def _tool_call_stub(
    tool_name: str = "code_edit",
    arguments: dict[str, Any] | None = None,
    final_reply: str = "done",
):
    """LLM stub: first call returns one tool_call; subsequent calls return text."""
    if arguments is None:
        arguments = {"path": "foo.py", "content": "x = 1"}
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
                                    "id": "call-vbr-001",
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


def _user_task(task_id: str = "t-vbr-1", text: str = "edit a file") -> Task:
    return Task(
        id=task_id,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _capture_tool_use(
    registry: HookRegistry | None = None,
) -> tuple[HookRegistry, list[dict[str, Any]]]:
    """Return a HookRegistry that captures every TOOL_USE payload."""
    if registry is None:
        registry = HookRegistry()
    captured: list[dict[str, Any]] = []

    async def _capture(payload: dict[str, Any]) -> None:
        captured.append(payload)

    registry.register(HookName.TOOL_USE, _capture, label="capture")
    return registry, captured


# ---------------------------------------------------------------------------
# 1. Integration: mutating tool + failing validator → BLOCK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_before_record_blocks_mutating_tool_on_failure() -> None:
    """When a validator fails for a mutating tool:
    - The tool IS called (validation is post-call, pre-record).
    - The tool message the next LLM turn sees contains the validation error.
    - The TOOL_USE hook fires with is_error=True.
    - The task still completes (BLOCK is per-call, not whole-task).
    """
    tool_args = {"path": "foo.py", "content": "x = 1"}
    stub = _tool_call_stub(tool_name="code_edit", arguments=tool_args, final_reply="I see the error")
    registry, captured_hooks = _capture_tool_use()
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=registry)
    proxy = _FakeToolProxy(result_text="edit applied")

    def _failing_validator(tool_name: str, arguments: dict, result: str) -> str | None:
        return "syntax error on line 1"

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_result_guardrails=[
            ValidateBeforeRecord(
                mutating_tools={"code_edit"},
                validate=_failing_validator,
            )
        ],
    )

    result = await persona.handle_task(_user_task())

    # Tool was called (validation is post-call).
    assert len(proxy.calls) == 1, "call_tool must have been invoked"
    assert proxy.calls[0]["name"] == "code_edit"

    # Task completes (BLOCK is per-call, not whole-task).
    assert result.status.state == "completed", result.status

    # The second LLM call must see the validation error in the tool message.
    second_call_messages: list[dict[str, Any]] = stub.captured[1]  # type: ignore[attr-defined]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert "validation failed for code_edit" in content, f"expected error in: {content!r}"
    assert "syntax error on line 1" in content, f"expected error detail in: {content!r}"
    assert tool_msgs[0]["tool_call_id"] == "call-vbr-001"

    # TOOL_USE hook must have fired with is_error=True.
    assert len(captured_hooks) == 1, captured_hooks
    hook_row = captured_hooks[0]
    assert hook_row["is_error"] is True, f"expected is_error=True, got {hook_row}"
    assert "validation failed" in hook_row["result_summary"], hook_row


# ---------------------------------------------------------------------------
# 2. Non-mutating tools pass through untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_before_record_passes_non_mutating_tool() -> None:
    """A non-mutating tool must not be validated; the real result passes through."""
    tool_args = {"path": "foo.py"}
    stub = _tool_call_stub(
        tool_name="read_file", arguments=tool_args, final_reply="got content"
    )
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="file contents here")

    validator_calls: list[tuple[str, dict, str]] = []

    def _recording_validator(tool_name: str, arguments: dict, result: str) -> str | None:
        validator_calls.append((tool_name, arguments, result))
        return "should not be called"

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_result_guardrails=[
            ValidateBeforeRecord(
                mutating_tools={"code_edit"},  # read_file is NOT in this set
                validate=_recording_validator,
            )
        ],
    )

    result = await persona.handle_task(_user_task())

    assert result.status.state == "completed"
    # Tool was called and the result passed through.
    assert len(proxy.calls) == 1
    # Validator must NOT have been called for non-mutating tool.
    assert validator_calls == [], f"validator called unexpectedly: {validator_calls}"

    # The second LLM call must have seen the real result, not a block.
    second_call_messages: list[dict[str, Any]] = stub.captured[1]  # type: ignore[attr-defined]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "file contents here"


# ---------------------------------------------------------------------------
# 3. Validator receives correct tool_name / arguments / result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_receives_correct_args() -> None:
    """The validate callable must receive the exact tool_name, arguments, and
    result string that the tool call produced."""
    tool_args = {"path": "bar.py", "content": "y = 2"}
    stub = _tool_call_stub(tool_name="code_edit", arguments=tool_args)
    llm_client = LiteLLMClient(acompletion_fn=stub, hook_registry=HookRegistry())
    proxy = _FakeToolProxy(result_text="edit applied successfully")

    recorded: list[tuple[str, dict[str, Any], str]] = []

    def _recording_validator(tool_name: str, arguments: dict[str, Any], result: str) -> str | None:
        recorded.append((tool_name, arguments, result))
        return None  # pass

    persona = LLMPersona(
        name="tester",
        model="test/model",
        llm_client=llm_client,
        system_prompt="test",
        tool_proxy=proxy,
        tool_result_guardrails=[
            ValidateBeforeRecord(
                mutating_tools={"code_edit"},
                validate=_recording_validator,
            )
        ],
    )

    result = await persona.handle_task(_user_task())

    assert result.status.state == "completed"
    assert len(recorded) == 1, f"validator not called: {recorded}"
    got_name, got_args, got_result = recorded[0]
    assert got_name == "code_edit"
    assert got_args == tool_args
    assert got_result == "edit applied successfully"


# ---------------------------------------------------------------------------
# 4. Unit isolation: check() with a hand-built GuardrailContext
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unit_block_with_hand_built_context() -> None:
    """check() returns BLOCK when ctx.tool_name is mutating and validator fails."""
    guardrail = ValidateBeforeRecord(
        mutating_tools={"code_edit"},
        validate=lambda _name, _args, _result: "lint error: unused variable",
    )

    ctx = GuardrailContext(
        persona_name="tester",
        task_id="t-unit-1",
        phase="tool_result",
        tool_name="code_edit",
        tool_arguments={"path": "x.py"},
    )

    verdict = await guardrail.check("some result", ctx)

    assert verdict.type.value == "block"
    assert "validation failed for code_edit" in verdict.reason
    assert "lint error: unused variable" in verdict.reason


@pytest.mark.asyncio
async def test_unit_pass_when_tool_name_absent() -> None:
    """check() returns PASS when ctx.tool_name is None (non-tool-result phase)."""
    guardrail = ValidateBeforeRecord(
        mutating_tools={"code_edit"},
        validate=lambda _n, _a, _r: "should not be called",
    )

    ctx = GuardrailContext(
        persona_name="tester",
        task_id="t-unit-2",
        phase="output",
        tool_name=None,
    )

    verdict = await guardrail.check("some text", ctx)

    assert verdict.type.value == "pass"


@pytest.mark.asyncio
async def test_unit_pass_when_validator_returns_none() -> None:
    """check() returns PASS when the validator returns None (no error)."""
    guardrail = ValidateBeforeRecord(
        mutating_tools={"code_edit"},
        validate=lambda _n, _a, _r: None,
    )

    ctx = GuardrailContext(
        persona_name="tester",
        task_id="t-unit-3",
        phase="tool_result",
        tool_name="code_edit",
        tool_arguments={"path": "ok.py"},
    )

    verdict = await guardrail.check("edit applied", ctx)

    assert verdict.type.value == "pass"


# ---------------------------------------------------------------------------
# 5. Predicate form of mutating_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_predicate_form_matches_correctly() -> None:
    """mutating_tools accepts a Callable[[str], bool] predicate."""
    guardrail = ValidateBeforeRecord(
        mutating_tools=lambda name: name.startswith("write_"),
        validate=lambda _n, _a, _r: "blocked",
    )

    ctx_mutating = GuardrailContext(
        persona_name="tester",
        task_id="t-pred-1",
        phase="tool_result",
        tool_name="write_file",
        tool_arguments={},
    )
    ctx_non_mutating = GuardrailContext(
        persona_name="tester",
        task_id="t-pred-2",
        phase="tool_result",
        tool_name="read_file",
        tool_arguments={},
    )

    v_mutating = await guardrail.check("result", ctx_mutating)
    v_non_mutating = await guardrail.check("result", ctx_non_mutating)

    assert v_mutating.type.value == "block"
    assert v_non_mutating.type.value == "pass"


@pytest.mark.asyncio
async def test_validator_exception_blocks_instead_of_crashing() -> None:
    """A validator that raises must NOT crash the dispatch — check() catches it
    and fails closed with a BLOCK carrying the cause (cf. #57's crash-path fix)."""

    def _boom(tool_name: str, arguments: dict[str, Any], result: str) -> str | None:
        raise RuntimeError("linter exploded")

    guardrail = ValidateBeforeRecord(mutating_tools={"code_edit"}, validate=_boom)
    ctx = GuardrailContext(
        persona_name="p",
        task_id="t",
        phase="tool_result",
        tool_name="code_edit",
        tool_arguments={"path": "x.py"},
    )
    verdict = await guardrail.check("some result", ctx)
    assert verdict.type.value == "block"
    assert "raised" in verdict.reason
    assert "linter exploded" in verdict.reason
