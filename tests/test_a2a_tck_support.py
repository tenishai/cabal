"""The A2A compatibility kit's test-only persona and baseline check (#261)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from cabal.agents.tck import TckAgent
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.a2a.v1 import task_to_v1


def _task(message_id: str) -> Task:
    incoming = Message(role="user", parts=[TextPart(text="hi")], messageId=message_id)
    return Task(id="t-1", contextId="ctx", status=TaskStatus(state="submitted", message=incoming))


async def _run(message_id: str) -> dict:
    return task_to_v1(await TckAgent().handle_task(_task(message_id))).to_wire()


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["tck-complete-task-1", "anything-else"])
async def test_the_default_completes_with_hello(message_id: str) -> None:
    task = await _run(message_id)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["parts"] == [{"text": "Hello from TCK"}]
    assert task["contextId"] == "ctx"
    assert [m["parts"] for m in task["history"]] == [[{"text": "hi"}]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_id, part",
    [
        ("tck-artifact-text-1", {"text": "Generated text content"}),
        ("tck-artifact-data-1", {"data": {"key": "value", "count": 42}}),
        (
            "tck-artifact-file-url-1",
            {"url": "https://example.com/output.txt", "filename": "output.txt", "mediaType": "text/plain"},
        ),
        (
            "tck-artifact-file-1",
            {"raw": "R2VuZXJhdGVkIGZpbGUgY29udGVudA==", "filename": "output.txt", "mediaType": "text/plain"},
        ),
    ],
)
async def test_artifact_scenarios(message_id: str, part: dict) -> None:
    """The file-url prefix is checked before the file prefix it starts with."""
    task = await _run(message_id)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert [a["parts"] for a in task["artifacts"]] == [[part]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_id, state, text",
    [
        ("tck-input-required-1", "TASK_STATE_INPUT_REQUIRED", "input required"),
        ("tck-reject-task-1", "TASK_STATE_REJECTED", "rejected"),
        ("tck-message-response-1", "TASK_STATE_COMPLETED", "Direct message response"),
    ],
)
async def test_state_scenarios(message_id: str, state: str, text: str) -> None:
    task = await _run(message_id)
    assert task["status"]["state"] == state
    assert task["status"]["message"]["parts"] == [{"text": text}]


@pytest.mark.asyncio
async def test_the_tck_persona_is_off_by_default(app, client) -> None:
    assert app.state.dispatch_router.has_local("tck") is False


def _check_baseline():
    path = Path(__file__).parents[1] / "scripts" / "a2a_tck" / "check_baseline.py"
    spec = importlib.util.spec_from_file_location("check_baseline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_baseline_compare() -> None:
    report = {
        "per_requirement": {
            "A": {"status": "FAIL"},
            "B": {"status": "FAIL"},
            "C": {"status": "PASS"},
            "D": {"status": "SKIPPED"},
        }
    }
    unexpected, fixed = _check_baseline().compare(report, {"A": "known", "C": "was failing"})
    assert unexpected == ["B"]
    assert fixed == ["C"]


def test_every_baseline_entry_needs_a_reason() -> None:
    module = _check_baseline()
    assert module.unexplained({"A": "why", "B": "", "C": "  "}) == ["B", "C"]
    # The committed baseline itself.
    import json

    baseline = json.loads(module.BASELINE.read_text())
    assert module.unexplained(baseline) == []
