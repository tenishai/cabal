"""A2A v1.0 primitive types.

We use pydantic models so FastAPI can validate inbound JSON. Field names
match the Linux Foundation A2A spec where they exist; Cabal-specific
extensions live under `metadata.cabal_*`.

This is a subset — we add fields as the broker needs them. The pydantic
models permit extra fields so we don't fail validation on tomorrow's spec
additions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class Skill(_Open):
    id: str
    name: str
    description: str | None = None
    tags: list[str] | None = None


class AgentCapabilities(_Open):
    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False


class AgentCard(_Open):
    """Public capabilities document for an agent."""

    schema_version: str = Field(default="1.0", alias="schemaVersion")
    name: str
    description: str | None = None
    url: str
    version: str = "0.0.0"
    skills: list[Skill] = Field(default_factory=list)
    default_input_modes: list[str] = Field(
        default_factory=lambda: ["text"], alias="defaultInputModes"
    )
    default_output_modes: list[str] = Field(
        default_factory=lambda: ["text"], alias="defaultOutputModes"
    )
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    security_schemes: dict[str, Any] | None = Field(
        default=None, alias="securitySchemes"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, validate_by_name=True
    )


# ---- Task lifecycle types --------------------------------------------------


class TextPart(_Open):
    kind: Literal["text"] = "text"
    text: str


class DataPart(_Open):
    kind: Literal["data"] = "data"
    data: dict[str, Any]


Part = TextPart | DataPart


class Message(_Open):
    role: Literal["user", "agent"]
    parts: list[Part]
    message_id: str | None = Field(default=None, alias="messageId")


TaskState = Literal[
    "submitted",
    "working",
    "input-required",
    "completed",
    "failed",
    "canceled",
    "rejected",
    "auth-required",
]


class TaskStatus(_Open):
    state: TaskState
    message: Message | None = None


class Task(_Open):
    """A2A Task envelope.

    `metadata` carries Cabal-specific routing context (cabal_trace_id,
    cabal_span_id, cabal_generation, cabal_phase, cabal_session_token).
    """

    id: str
    context_id: str | None = Field(default=None, alias="contextId")
    status: TaskStatus
    history: list[Message] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
