"""Cabal reason-code tests."""

from __future__ import annotations

from cabal.core.a2a.codes import (
    CODE_BUDGET_EXHAUSTED,
    CODE_ESCALATION,
    CODE_HUMAN_INPUT,
    CODE_POLICY_VIOLATION,
    CabalReason,
    annotate,
    is_escalation,
    make_status,
    reason_code,
)
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart


def _task() -> Task:
    return Task(
        id="t-1",
        status=TaskStatus(
            state="working",
            message=Message(role="user", parts=[TextPart(text="x")]),
        ),
    )


def test_700_maps_to_input_required() -> None:
    s = make_status(CabalReason(code=CODE_HUMAN_INPUT, message="need more info"))
    assert s.state == "input-required"


def test_701_702_704_map_to_failed() -> None:
    for code in (CODE_POLICY_VIOLATION, CODE_BUDGET_EXHAUSTED, CODE_ESCALATION):
        s = make_status(CabalReason(code=code, message="x"))
        assert s.state == "failed"


def test_annotate_preserves_metadata_and_sets_code() -> None:
    t = _task()
    t.metadata["other"] = "kept"
    out = annotate(t, CabalReason(code=CODE_POLICY_VIOLATION, message="nope"))
    assert out.metadata["other"] == "kept"
    assert out.metadata["cabal_reason_code"] == CODE_POLICY_VIOLATION
    assert out.metadata["cabal_reason_message"] == "nope"
    assert out.status.state == "failed"


def test_annotate_escalation_carries_lineage() -> None:
    t = _task()
    out = annotate(
        t,
        CabalReason(
            code=CODE_ESCALATION, message="punting up", escalated_from="parent-task"
        ),
    )
    assert out.metadata["cabal_escalated_from"] == "parent-task"
    assert is_escalation(out)


def test_reason_code_returns_int_or_none() -> None:
    t = _task()
    assert reason_code(t) is None
    out = annotate(t, CabalReason(code=CODE_BUDGET_EXHAUSTED, message="over"))
    assert reason_code(out) == CODE_BUDGET_EXHAUSTED
