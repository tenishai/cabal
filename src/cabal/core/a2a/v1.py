"""A2A v1.0 wire types; internal Tasks to v1.0, and v1.0 messages inbound (#266).

cabal's internal types (`cabal.core.a2a.types`) are the pre-1.0 dialect the
legacy routes speak. This module is the v1.0 wire form, defined by the v1.0
proto (`a2a.proto`, A2A commit 1736957), serialized by spec 5.5 (camelCase
field names; enum values as their ProtoJSON strings, `TASK_STATE_COMPLETED`,
`ROLE_USER`) and spec 5.6.1 (timestamps as ISO-8601 UTC). Dispatch keeps using
the internal types; the v1.0 routes convert at the edge with the functions
below, so the dispatch core is not duplicated.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from cabal.core.a2a.types import DataPart, Message, Task, TextPart

TaskStateV1 = Literal[
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
]
RoleV1 = Literal["ROLE_USER", "ROLE_AGENT"]

# Internal (pre-1.0) state and role spellings to their v1.0 ProtoJSON names.
STATE_TO_V1: dict[str, TaskStateV1] = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "rejected": "TASK_STATE_REJECTED",
    "auth-required": "TASK_STATE_AUTH_REQUIRED",
}
STATE_FROM_V1: dict[TaskStateV1, str] = {v: k for k, v in STATE_TO_V1.items()}
_ROLE_TO_V1: dict[str, RoleV1] = {"user": "ROLE_USER", "agent": "ROLE_AGENT"}
_ROLE_FROM_V1 = {v: k for k, v in _ROLE_TO_V1.items()}


class _V1(BaseModel):
    """camelCase on the wire, snake_case in Python; unknown fields ignored."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    def to_wire(self) -> dict[str, Any]:
        """The v1.0 JSON object: aliased names, unset optional fields omitted."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class PartV1(_V1):
    """One piece of content: exactly one of `text`, `raw`, `url` or `data`."""

    text: str | None = None
    raw: bytes | None = None
    url: str | None = None
    data: Any = None
    metadata: dict[str, Any] | None = None
    filename: str | None = None
    media_type: str | None = None

    @model_validator(mode="after")
    def _exactly_one_content(self) -> PartV1:
        present = [f for f in ("text", "raw", "url") if getattr(self, f) is not None]
        if "data" in self.model_fields_set:
            present.append("data")
        if len(present) != 1:
            raise ValueError(
                f"a Part holds exactly one of text, raw, url or data; got {present or 'none'}"
            )
        return self

    @field_serializer("raw")
    def _raw_as_base64(self, raw: bytes | None) -> str | None:
        return None if raw is None else base64.b64encode(raw).decode("ascii")

    @field_validator("raw", mode="before")
    @classmethod
    def _raw_from_base64(cls, value: Any) -> Any:
        """The wire carries `raw` as base64 text, so decode it on the way in;
        without this pydantic keeps the base64 text itself as the bytes and a
        round trip double-encodes."""
        if isinstance(value, str):
            try:
                return base64.b64decode(value, validate=True)
            except binascii.Error as exc:
                raise ValueError("raw must be base64") from exc
        return value

    @model_serializer(mode="wrap")
    def _keep_explicit_null_data(self, handler: SerializerFunctionWrapHandler) -> Any:
        """An explicit `data: null` is content, not absence, so put it back
        after `exclude_none` strips it. As a model serializer this runs
        wherever the part is serialized, nested in a message or artifact too."""
        out = handler(self)
        if isinstance(out, dict) and "data" in self.model_fields_set and "data" not in out:
            out["data"] = None
        return out


class MessageV1(_V1):
    message_id: str
    context_id: str | None = None
    task_id: str | None = None
    role: RoleV1
    parts: list[PartV1] = Field(min_length=1)
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None
    reference_task_ids: list[str] | None = None


class TaskStatusV1(_V1):
    state: TaskStateV1
    message: MessageV1 | None = None
    timestamp: datetime | None = None

    @field_serializer("timestamp")
    def _utc_iso(self, ts: datetime | None) -> str | None:
        return None if ts is None else iso_utc(ts)


class ArtifactV1(_V1):
    artifact_id: str
    name: str | None = None
    description: str | None = None
    parts: list[PartV1] = Field(min_length=1)
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None


class TaskV1(_V1):
    id: str
    context_id: str | None = None
    status: TaskStatusV1
    artifacts: list[ArtifactV1] | None = None
    history: list[MessageV1] | None = None
    metadata: dict[str, Any] | None = None


class TaskStatusUpdateEventV1(_V1):
    task_id: str
    context_id: str
    status: TaskStatusV1
    metadata: dict[str, Any] | None = None


class TaskArtifactUpdateEventV1(_V1):
    task_id: str
    context_id: str
    artifact: ArtifactV1
    append: bool | None = None
    last_chunk: bool | None = None
    metadata: dict[str, Any] | None = None


class SendMessageConfigurationV1(_V1):
    accepted_output_modes: list[str] | None = None
    # Kept, not modelled: cabal declares push notifications unsupported, and
    # the v1.0 route is to answer a request carrying this with
    # PushNotificationNotSupportedError (#268) rather than drop it.
    task_push_notification_config: dict[str, Any] | None = None
    history_length: int | None = Field(default=None, ge=0)
    return_immediately: bool | None = None


class SendMessageRequestV1(_V1):
    tenant: str | None = None
    message: MessageV1
    configuration: SendMessageConfigurationV1 | None = None
    metadata: dict[str, Any] | None = None


class GetTaskRequestV1(_V1):
    tenant: str | None = None
    id: str
    history_length: int | None = Field(default=None, ge=0)


class ListTasksRequestV1(_V1):
    tenant: str | None = None
    context_id: str | None = None
    # TASK_STATE_UNSPECIFIED is the proto default: no status filter.
    status: TaskStateV1 | Literal["TASK_STATE_UNSPECIFIED"] | None = None
    page_size: int | None = Field(default=None, ge=1, le=100)
    page_token: str | None = None
    history_length: int | None = Field(default=None, ge=0)
    status_timestamp_after: datetime | None = None
    include_artifacts: bool | None = None


class ListTasksResponseV1(_V1):
    tasks: list[TaskV1]
    # "" on the final page; always present (spec 3.1.4).
    next_page_token: str
    page_size: int
    total_size: int


class CancelTaskRequestV1(_V1):
    tenant: str | None = None
    id: str
    metadata: dict[str, Any] | None = None


class SendMessageResponseV1(_V1):
    """`SendMessage`'s result: exactly one of a Task or a direct Message."""

    task: TaskV1 | None = None
    message: MessageV1 | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SendMessageResponseV1:
        if (self.task is None) == (self.message is None):
            raise ValueError("a SendMessageResponse holds exactly one of task or message")
        return self


# ---- conversion ------------------------------------------------------------


def iso_utc(ts: datetime) -> str:
    """ISO-8601 in UTC with a `Z` suffix; a naive datetime is taken as UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stable_id(task_id: str, slot: str) -> str:
    """A message or artifact id that is the same every time the task is read.

    v1.0 requires `messageId` and `artifactId`, and internal messages may have
    neither. A random id would change between two `GetTask` calls for the same
    message, so derive one from the task id and a slot instead. History and
    artifacts use their position as the slot. The status message has no
    position, so its slot is the state plus the message's content: the same
    status message keeps its id, and a different one gets a new id.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cabal:{task_id}:{slot}"))


# The wire keys of a v1.0 Part, for a kindless dict that is already one.
_V1_PART_KEYS = frozenset({"text", "raw", "url", "data", "metadata", "filename", "mediaType"})

# The keys a free-form part dict may have, per kind, and still convert field by field.
_PART_KEYS = {
    "text": frozenset({"kind", "text", "metadata"}),
    "data": frozenset({"kind", "data", "metadata"}),
}


def part_to_v1(part: Any) -> PartV1:
    """An internal part to v1.0, keeping its `text` or `data` and its `metadata`.

    A free-form part dict converts only when its kind is `text` (with a
    string `text`) or `data` (with a `data` key) and every key is one that
    kind keeps (`_PART_KEYS`). Anything else raises TypeError, so its
    artifact travels whole as one data part (see _artifact_to_v1) instead
    of losing a key: an extra key (including `data` on a text part or `text`
    on a data part), a file part, or a kind cabal does not know. A dict with
    no `kind` that is already a valid v1.0 Part (only v1.0 Part keys, one
    content field) is taken as that Part; any other kindless dict travels
    whole as this part's data.
    """
    if isinstance(part, (TextPart, DataPart)):
        metadata = (part.model_extra or {}).get("metadata")
        if isinstance(part, TextPart):
            return PartV1(text=part.text, metadata=metadata)
        return PartV1(data=part.data, metadata=metadata)
    if isinstance(part, dict):  # a part kept as raw JSON in a free-form artifact
        kind = part.get("kind")
        if kind is None:
            if set(part) <= _V1_PART_KEYS:
                try:  # already a v1.0 Part, e.g. {"text": "x"}
                    return PartV1.model_validate(part)
                except ValueError:
                    pass
            return PartV1(data=part)
        allowed = _PART_KEYS.get(kind)
        if allowed is None:
            raise TypeError(f"cannot convert a part of kind {kind!r}")
        if not set(part) <= allowed:
            raise TypeError(
                f"a {kind} part with keys {sorted(set(part) - allowed)} does not convert"
            )
        if kind == "text" and isinstance(part.get("text"), str):
            return PartV1(text=part["text"], metadata=part.get("metadata"))
        if kind == "data" and "data" in part:
            return PartV1(data=part["data"], metadata=part.get("metadata"))
        raise TypeError(f"a {kind} part without its {kind} does not convert")
    raise TypeError(f"cannot convert part of type {type(part).__name__}")


def message_to_v1(message: Message, *, task_id: str, slot: str) -> MessageV1:
    return MessageV1(
        message_id=message.message_id or _stable_id(task_id, slot),
        role=_ROLE_TO_V1[message.role],
        parts=[part_to_v1(p) for p in message.parts] or [PartV1(text="")],
    )


# The keys of a v1.0 Artifact, as an internal artifact dict spells them.
_ARTIFACT_KEYS = frozenset({"artifactId", "name", "description", "parts", "metadata", "extensions"})


def _artifact_to_v1(artifact: dict[str, Any], *, task_id: str, index: int) -> ArtifactV1:
    """Internal artifacts are free-form dicts.

    One is in "the A2A shape" when every key is a v1.0 Artifact field
    (`_ARTIFACT_KEYS`) and it has a non-empty `parts` list; it then converts
    field by field. Anything else travels whole as a single data part: an
    artifact with any other key (so no key is dropped), one without parts,
    and one whose parts or name do not convert (see part_to_v1 for when a
    part converts). So no key of an artifact or of its parts is dropped, and
    one odd artifact never fails the read of the whole Task.
    """
    artifact_id = str(artifact.get("artifactId") or _stable_id(task_id, f"artifact-{index}"))
    raw_parts = artifact.get("parts")
    if set(artifact) <= _ARTIFACT_KEYS and isinstance(raw_parts, list) and raw_parts:
        try:
            return ArtifactV1(
                artifact_id=artifact_id,
                name=artifact.get("name"),
                description=artifact.get("description"),
                parts=[part_to_v1(p) for p in raw_parts],
                metadata=artifact.get("metadata"),
                extensions=artifact.get("extensions"),
            )
        except (TypeError, ValueError):  # pydantic's ValidationError is a ValueError
            pass
    return ArtifactV1(artifact_id=artifact_id, parts=[PartV1(data=artifact)])


# cabal's own metadata keys a v1.0 client is told, under their v1.0 names:
# why the task ended the way it did (codes.py).
_PUBLIC_CABAL_KEYS = {
    "cabal_reason_code": "cabalReasonCode",
    "cabal_reason_message": "cabalReasonMessage",
    "cabal_escalated_from": "cabalEscalatedFrom",
    # A scheduled task's start time and how late it started (issue 406), and
    # whether a restart cut it off (issue 405): a caller acts on all three.
    "cabal_not_before": "cabalNotBefore",
    "cabal_started_late_s": "cabalStartedLateS",
    "cabal_interrupted": "cabalInterrupted",
    # A waiting agent (reason 705, issue 406): what it waits for, and the
    # continuation cabal scheduled for it, or the task it continues.
    "cabal_waiting": "cabalWaiting",
    "cabal_continued_by": "cabalContinuedBy",
    "cabal_continues": "cabalContinues",
    "cabal_wait_count": "cabalWaitCount",
}


def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Task metadata as a v1.0 client sees it. The reason keys are renamed to
    camelCase, as v1.0 field names are (DM-SERIAL-001). cabal's other
    `cabal_*` keys (trace and span ids, the delegation budget, session flags,
    the routed agent) are internal bookkeeping and are left out. The
    `cabal_` prefix is reserved for cabal: a client's own key with it is
    dropped or renamed the same way. Every other key passes through as
    sent."""
    public: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in _PUBLIC_CABAL_KEYS:
            public[_PUBLIC_CABAL_KEYS[key]] = value
        elif not key.startswith("cabal_"):
            public[key] = value
    return public or None


def task_to_v1(
    task: Task, *, timestamp: datetime | None = None, history_length: int | None = None
) -> TaskV1:
    """Internal Task to v1.0. `history_length` keeps the most recent messages."""
    history = task.history
    if history_length is not None:
        history = history[max(0, len(history) - history_length) :] if history_length else []
    offset = len(task.history) - len(history)
    status_message = (
        message_to_v1(
            task.status.message,
            task_id=task.id,
            slot="status:"
            + task.status.state
            + ":"
            + task.status.message.model_dump_json(by_alias=True, exclude_none=True),
        )
        if task.status.message is not None
        else None
    )
    return TaskV1(
        id=task.id,
        context_id=task.context_id,
        status=TaskStatusV1(
            state=STATE_TO_V1[task.status.state], message=status_message, timestamp=timestamp
        ),
        artifacts=[
            _artifact_to_v1(a, task_id=task.id, index=i) for i, a in enumerate(task.artifacts)
        ]
        or None,
        history=[
            message_to_v1(m, task_id=task.id, slot=f"history-{offset + i}")
            for i, m in enumerate(history)
        ]
        or None,
        metadata=_public_metadata(task.metadata),
    )


def conversation_to_v1(
    *,
    task_id: str,
    state: str,
    result_json: dict[str, Any] | None,
    updated_at: datetime | None,
    history_length: int | None = None,
) -> TaskV1:
    """A `conversations` row to a v1.0 Task.

    A row written by a successful dispatch carries its Task in
    `result_json`, whatever state that Task is in (`on_dispatch_success`).
    A row without one has only a state: either still in flight, or ended by
    a dispatch failure (`on_dispatch_failure` sets `state="failed"` and
    stores no Task). The legacy `tasks/get` answers such a row with `{"id",
    "status": {"state": <legacy string>}}`; this gives the v1.0 equivalent,
    with the row's last update as the status timestamp. A caller that wants
    a status message for the failed case adds one.
    """
    if result_json is None:
        return TaskV1(
            id=task_id, status=TaskStatusV1(state=STATE_TO_V1[state], timestamp=updated_at)
        )
    return task_to_v1(
        Task.model_validate(result_json), timestamp=updated_at, history_length=history_length
    )


class UnsupportedPartError(ValueError):
    """A v1.0 part cabal's internal model cannot carry (`raw` or `url`)."""


def message_from_v1(message: MessageV1) -> Message:
    """Inbound v1.0 message to the internal form personas consume.

    Internal messages carry text and JSON-object data only. A `raw` or `url`
    part raises UnsupportedPartError, which the v1.0 route is to report as
    ContentTypeNotSupportedError (#274). Non-object `data` is wrapped as
    `{"value": ...}` so it survives as a data part.
    """
    parts: list[TextPart | DataPart] = []
    for p in message.parts:
        if p.text is not None:
            parts.append(TextPart(text=p.text))
        elif "data" in p.model_fields_set:
            parts.append(DataPart(data=p.data if isinstance(p.data, dict) else {"value": p.data}))
        else:
            kind = "raw" if p.raw is not None else "url"
            raise UnsupportedPartError(f"a {kind} part is not supported")
    return Message(role=_ROLE_FROM_V1[message.role], parts=parts, messageId=message.message_id)
