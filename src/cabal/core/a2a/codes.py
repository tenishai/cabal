"""Cabal reason codes layered over A2A v1.0 task states.

A2A only defines coarse outcomes (`failed`, `input-required`, etc.). Cabal
agents need to be more specific so callers can decide whether to retry,
escalate, or surface to the operator. We carry the specific reason in
`task.metadata.cabal_reason_code` while still using A2A's canonical state
on the wire.

Code families:
    700  human-input-required  (operator clarification needed)
    701  policy-violation       (safety-tier guard refused; not retryable)
    702  budget-exhausted       (token budget overran)
    703  tool-blocked           (a tool or rule refused the work; same name and
                                 meaning as daedalus's ReasonCode.TOOL_BLOCKED)
    704  escalation             (subordinate handed back to caller; primus
                                 should pass to operator)

Numbers are intentionally non-contiguous to leave room. State on the wire
mirrors A2A: 700 → `input-required`, 701/702/703 → `failed`, 704 → `failed`.
"""

from __future__ import annotations

from dataclasses import dataclass

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart

CODE_HUMAN_INPUT = 700
CODE_POLICY_VIOLATION = 701
CODE_BUDGET_EXHAUSTED = 702
CODE_TOOL_BLOCKED = 703
CODE_ESCALATION = 704


_STATE_FOR: dict[int, str] = {
    CODE_HUMAN_INPUT: "input-required",
    CODE_POLICY_VIOLATION: "failed",
    CODE_BUDGET_EXHAUSTED: "failed",
    CODE_TOOL_BLOCKED: "failed",
    CODE_ESCALATION: "failed",
}


@dataclass(slots=True)
class CabalReason:
    code: int
    message: str
    escalated_from: str | None = None  # task_id of the parent, for 704 lineage


def make_status(reason: CabalReason) -> TaskStatus:
    """Build a TaskStatus with the canonical A2A state for `reason.code`."""
    state = _STATE_FOR.get(reason.code, "failed")
    return TaskStatus(
        state=state,  # type: ignore[arg-type]
        message=Message(role="agent", parts=[TextPart(text=reason.message)]),
    )


def annotate(task: Task, reason: CabalReason) -> Task:
    """Return a copy of `task` with cabal_reason_code in metadata and
    its status set to the A2A equivalent."""
    new_metadata = dict(task.metadata)
    new_metadata["cabal_reason_code"] = reason.code
    new_metadata["cabal_reason_message"] = reason.message
    if reason.escalated_from is not None:
        new_metadata["cabal_escalated_from"] = reason.escalated_from
    return task.model_copy(
        update={"status": make_status(reason), "metadata": new_metadata}
    )


def is_escalation(task: Task) -> bool:
    return task.metadata.get("cabal_reason_code") == CODE_ESCALATION


def reason_code(task: Task) -> int | None:
    value = task.metadata.get("cabal_reason_code")
    return int(value) if value is not None else None
