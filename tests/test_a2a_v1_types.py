"""A2A v1.0 wire types and conversion (#266).

Expected JSON follows the v1.0 spec (A2A commit 1736957): camelCase names,
ProtoJSON enum strings, ISO-8601 UTC timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cabal.core.a2a.types import DataPart, Message, Task, TaskStatus, TextPart
from cabal.core.a2a.v1 import (
    STATE_TO_V1,
    MessageV1,
    PartV1,
    SendMessageConfigurationV1,
    SendMessageRequestV1,
    SendMessageResponseV1,
    TaskArtifactUpdateEventV1,
    TaskStatusUpdateEventV1,
    UnsupportedPartError,
    conversation_to_v1,
    iso_utc,
    message_from_v1,
    task_to_v1,
)

TS = datetime(2026, 10, 4, 12, 30, 0, tzinfo=UTC)


def _task(**kw) -> Task:
    base = dict(
        id="t1",
        contextId="c1",
        status=TaskStatus(
            state="completed", message=Message(role="agent", parts=[TextPart(text="done")])
        ),
    )
    base.update(kw)
    return Task(**base)


# ---- enums and names -------------------------------------------------------


def test_internal_metadata_is_not_on_the_wire() -> None:
    """DM-SERIAL-001: v1.0 field names are camelCase. cabal's reason keys are
    renamed; its other bookkeeping keys are left out; a client's own pass."""
    task = _task(
        status=TaskStatus(state="failed"),
        metadata={
            "cabal_reason_code": 702,
            "cabal_reason_message": "budget",
            "cabal_trace_id": "abc",
            "cabal_delegation_budget": {"used": 3},
            "clientKey": "kept",
        },
    )
    wire = task_to_v1(task).to_wire()
    assert wire["metadata"] == {
        "cabalReasonCode": 702,
        "cabalReasonMessage": "budget",
        "clientKey": "kept",
    }


def test_every_internal_state_has_a_v1_name() -> None:
    internal = set(Task.model_fields["status"].annotation.model_fields["state"].annotation.__args__)
    assert set(STATE_TO_V1) == internal
    assert all(v.startswith("TASK_STATE_") for v in STATE_TO_V1.values())


def test_completed_task_matches_the_spec_shape() -> None:
    wire = task_to_v1(_task(), timestamp=TS).to_wire()
    assert wire["id"] == "t1"
    assert wire["contextId"] == "c1"
    assert wire["status"]["state"] == "TASK_STATE_COMPLETED"
    assert wire["status"]["timestamp"] == "2026-10-04T12:30:00Z"
    msg = wire["status"]["message"]
    assert msg["role"] == "ROLE_AGENT"
    assert msg["parts"] == [{"text": "done"}]
    assert msg["messageId"]  # required in v1.0 even when the internal message had none
    assert "context_id" not in wire and "message_id" not in msg  # camelCase only


@pytest.mark.parametrize("internal, v1", sorted(STATE_TO_V1.items()))
def test_state_mapping(internal: str, v1: str) -> None:
    task = _task(status=TaskStatus(state=internal))
    assert task_to_v1(task).to_wire()["status"]["state"] == v1


def test_reason_code_metadata_travels_with_the_mapped_state() -> None:
    task = _task(status=TaskStatus(state="input-required"), metadata={"cabal_reason_code": 700})
    wire = task_to_v1(task).to_wire()
    assert wire["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert wire["metadata"] == {"cabalReasonCode": 700}


# ---- timestamps ------------------------------------------------------------


def test_timestamps_are_utc_with_z() -> None:
    plus_two = datetime(2026, 10, 4, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    assert iso_utc(plus_two) == "2026-10-04T12:30:00Z"
    assert iso_utc(datetime(2026, 10, 4, 12, 30)) == "2026-10-04T12:30:00Z"  # naive is UTC


# ---- parts -----------------------------------------------------------------


def test_part_requires_exactly_one_content() -> None:
    with pytest.raises(ValidationError):
        PartV1()
    with pytest.raises(ValidationError):
        PartV1(text="a", url="https://x")


def test_part_raw_is_base64_and_data_null_is_kept() -> None:
    assert PartV1(raw=b"\x00\x01", media_type="application/octet-stream").to_wire() == {
        "raw": "AAE=",
        "mediaType": "application/octet-stream",
    }
    assert PartV1(data=None).to_wire() == {"data": None}


def test_internal_parts_convert() -> None:
    task = _task(
        status=TaskStatus(
            state="completed",
            message=Message(role="agent", parts=[TextPart(text="hi"), DataPart(data={"k": 1})]),
        )
    )
    parts = task_to_v1(task).to_wire()["status"]["message"]["parts"]
    assert parts == [{"text": "hi"}, {"data": {"k": 1}}]


# ---- required fields and stable ids ----------------------------------------


def test_ids_are_stable_across_reads() -> None:
    task = _task(
        history=[Message(role="user", parts=[TextPart(text="q")])],
        artifacts=[{"note": "free-form artifact"}],
    )
    first, second = task_to_v1(task).to_wire(), task_to_v1(task).to_wire()
    assert first["history"][0]["messageId"] == second["history"][0]["messageId"]
    assert first["artifacts"][0]["artifactId"] == second["artifacts"][0]["artifactId"]
    assert first["status"]["message"]["messageId"] == second["status"]["message"]["messageId"]


def test_existing_message_id_is_kept() -> None:
    task = _task(history=[Message(role="user", parts=[TextPart(text="q")], messageId="m-1")])
    assert task_to_v1(task).to_wire()["history"][0]["messageId"] == "m-1"


def test_free_form_artifact_travels_as_one_data_part() -> None:
    task = _task(artifacts=[{"note": "x"}])
    art = task_to_v1(task).to_wire()["artifacts"][0]
    assert art["parts"] == [{"data": {"note": "x"}}]
    assert art["artifactId"]


def test_every_emitted_message_has_its_required_fields() -> None:
    """DM-SERIAL-004: Task.id, status.state, and messageId/role/parts on messages."""
    task = _task(
        history=[Message(role="user", parts=[]), Message(role="agent", parts=[TextPart(text="a")])]
    )
    wire = task_to_v1(task, timestamp=TS).to_wire()
    assert wire["id"] and wire["status"]["state"]
    for msg in [wire["status"]["message"], *wire["history"]]:
        assert msg["messageId"] and msg["role"] and msg["parts"]


# ---- history length --------------------------------------------------------


@pytest.mark.parametrize("length, expected", [(None, 3), (0, 0), (2, 2), (5, 3)])
def test_history_length_keeps_the_most_recent(length, expected) -> None:
    history = [Message(role="user", parts=[TextPart(text=str(i))]) for i in range(3)]
    wire = task_to_v1(_task(history=history), history_length=length).to_wire()
    got = wire.get("history", [])
    assert len(got) == expected
    if expected:
        assert got[-1]["parts"] == [{"text": "2"}]  # the newest is last


def test_trimmed_history_keeps_each_message_id() -> None:
    history = [Message(role="user", parts=[TextPart(text=str(i))]) for i in range(3)]
    full = task_to_v1(_task(history=history)).to_wire()["history"]
    trimmed = task_to_v1(_task(history=history), history_length=1).to_wire()["history"]
    assert trimmed[0]["messageId"] == full[-1]["messageId"]


# ---- conversation rows -----------------------------------------------------


def test_in_flight_row_converts_from_state_alone() -> None:
    wire = conversation_to_v1(
        task_id="t9", state="working", result_json=None, updated_at=TS
    ).to_wire()
    assert wire == {
        "id": "t9",
        "status": {"state": "TASK_STATE_WORKING", "timestamp": "2026-10-04T12:30:00Z"},
    }


def test_finished_row_converts_from_result_json() -> None:
    result = _task().model_dump(by_alias=True, exclude_none=True)
    wire = conversation_to_v1(
        task_id="t1", state="completed", result_json=result, updated_at=TS
    ).to_wire()
    assert wire["status"]["state"] == "TASK_STATE_COMPLETED"
    assert wire["status"]["timestamp"] == "2026-10-04T12:30:00Z"


# ---- inbound ---------------------------------------------------------------


def test_send_message_request_parses_spec_json() -> None:
    req = SendMessageRequestV1.model_validate(
        {
            "tenant": "mason",
            "message": {
                "messageId": "m-1",
                "role": "ROLE_USER",
                "parts": [{"text": "hello"}],
            },
            "configuration": {"historyLength": 2, "returnImmediately": False},
        }
    )
    assert req.tenant == "mason"
    assert req.configuration.history_length == 2
    internal = message_from_v1(req.message)
    assert internal.role == "user"
    assert internal.message_id == "m-1"
    assert internal.parts[0].text == "hello"


@pytest.mark.parametrize(
    "bad",
    [
        {"role": "ROLE_USER", "parts": [{"text": "x"}]},  # no messageId
        {"messageId": "m", "parts": [{"text": "x"}]},  # no role
        {"messageId": "m", "role": "ROLE_USER", "parts": []},  # empty parts
        {"messageId": "m", "role": "user", "parts": [{"text": "x"}]},  # pre-1.0 role spelling
    ],
)
def test_invalid_messages_are_rejected(bad) -> None:
    with pytest.raises(ValidationError):
        MessageV1.model_validate(bad)


def test_data_part_inbound_and_non_object_data_is_wrapped() -> None:
    msg = MessageV1(
        message_id="m", role="ROLE_USER", parts=[PartV1(data={"a": 1}), PartV1(data=[1, 2])]
    )
    internal = message_from_v1(msg)
    assert [p.data for p in internal.parts] == [{"a": 1}, {"value": [1, 2]}]


@pytest.mark.parametrize("part", [PartV1(raw=b"x"), PartV1(url="https://example.com/f")])
def test_raw_and_url_parts_are_unsupported(part) -> None:
    with pytest.raises(UnsupportedPartError):
        message_from_v1(MessageV1(message_id="m", role="ROLE_USER", parts=[part]))


# ---- update events ---------------------------------------------------------


def test_update_events_require_task_and_context_ids() -> None:
    with pytest.raises(ValidationError):
        TaskStatusUpdateEventV1.model_validate({"status": {"state": "TASK_STATE_WORKING"}})
    event = TaskArtifactUpdateEventV1.model_validate(
        {
            "taskId": "t1",
            "contextId": "c1",
            "artifact": {"artifactId": "a1", "parts": [{"text": "chunk"}]},
            "append": True,
            "lastChunk": False,
        }
    )
    assert event.to_wire()["lastChunk"] is False


# ---- review fixes (PR #278) -------------------------------------------------


@pytest.mark.parametrize(
    "artifact",
    [
        {"parts": ["bare string"]},  # parts that do not convert
        {"name": 7, "parts": [{"kind": "text", "text": "x"}]},  # a non-string name
        {"note": "no parts at all"},
    ],
)
def test_an_odd_artifact_travels_whole_and_never_fails_the_task(artifact) -> None:
    wire = task_to_v1(_task(artifacts=[artifact])).to_wire()
    art = wire["artifacts"][0]
    assert art["artifactId"]
    assert art["parts"] == [{"data": artifact}]


def test_a2a_shaped_artifact_converts_field_by_field() -> None:
    artifact = {"artifactId": "a1", "name": "report", "parts": [{"kind": "text", "text": "r"}]}
    art = task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]
    assert art == {"artifactId": "a1", "name": "report", "parts": [{"text": "r"}]}


def test_push_notification_config_is_kept_not_dropped() -> None:
    cfg = SendMessageConfigurationV1.model_validate(
        {"taskPushNotificationConfig": {"url": "https://x"}, "historyLength": 1}
    )
    assert cfg.task_push_notification_config == {"url": "https://x"}
    assert cfg.to_wire()["taskPushNotificationConfig"] == {"url": "https://x"}


def test_send_message_response_holds_exactly_one() -> None:
    task = task_to_v1(_task())
    msg = MessageV1(message_id="m", role="ROLE_AGENT", parts=[PartV1(text="hi")])
    assert "task" in SendMessageResponseV1(task=task).to_wire()
    assert "message" in SendMessageResponseV1(message=msg).to_wire()
    with pytest.raises(ValidationError):
        SendMessageResponseV1()
    with pytest.raises(ValidationError):
        SendMessageResponseV1(task=task, message=msg)


def test_distinct_status_messages_get_distinct_ids() -> None:
    def status_id(state: str, text: str) -> str:
        task = _task(
            status=TaskStatus(
                state=state, message=Message(role="agent", parts=[TextPart(text=text)])
            )
        )
        return task_to_v1(task).to_wire()["status"]["message"]["messageId"]

    assert status_id("working", "step 1") == status_id("working", "step 1")
    assert status_id("working", "step 1") != status_id("working", "step 2")
    assert status_id("working", "x") != status_id("completed", "x")


def test_a2a_shaped_artifact_keeps_metadata_and_extensions() -> None:
    artifact = {
        "artifactId": "a1",
        "parts": [{"kind": "text", "text": "r"}],
        "metadata": {"k": "v"},
        "extensions": ["https://example.com/ext"],
    }
    art = task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]
    assert art["metadata"] == {"k": "v"}
    assert art["extensions"] == ["https://example.com/ext"]


def test_raw_round_trips_through_its_own_wire_form() -> None:
    """Review of PR #278: raw was base64 on the way out but not decoded on the way in."""
    original = PartV1(raw=b"\x00\x01\xff")
    back = PartV1.model_validate(original.to_wire())
    assert back.raw == b"\x00\x01\xff"
    assert back.to_wire() == original.to_wire()
    assert PartV1.model_validate_json('{"raw": "AAE="}').raw == b"\x00\x01"


def test_raw_that_is_not_base64_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PartV1.model_validate({"raw": "not base64!"})


def test_artifact_with_a_file_part_travels_whole() -> None:
    artifact = {"artifactId": "a1", "parts": [{"kind": "file", "file": {"uri": "https://x/f"}}]}
    art = task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]
    assert art["parts"] == [{"data": artifact}]


def test_failed_row_without_a_task_converts_to_failed() -> None:
    wire = conversation_to_v1(
        task_id="t2", state="failed", result_json=None, updated_at=TS
    ).to_wire()
    assert wire["status"]["state"] == "TASK_STATE_FAILED"


def test_artifact_with_an_extra_key_travels_whole_so_nothing_is_dropped() -> None:
    """Review of PR #278: an A2A-looking artifact with one more key lost that key."""
    artifact = {
        "artifactId": "a1",
        "name": "r",
        "parts": [{"kind": "text", "text": "x"}],
        "note": "EXTRA",
    }
    art = task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]
    assert art["artifactId"] == "a1"
    assert art["parts"] == [{"data": artifact}]


def test_part_metadata_is_kept() -> None:
    artifact = {
        "artifactId": "a1",
        "parts": [
            {"kind": "text", "text": "x", "metadata": {"k": "v"}},
            {"kind": "data", "data": {"n": 1}, "metadata": {"m": 2}},
        ],
    }
    parts = task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]["parts"]
    assert parts == [
        {"text": "x", "metadata": {"k": "v"}},
        {"data": {"n": 1}, "metadata": {"m": 2}},
    ]


def test_internal_part_metadata_is_kept() -> None:
    task = _task(
        status=TaskStatus(
            state="completed",
            message=Message(role="agent", parts=[TextPart(text="hi", metadata={"k": "v"})]),
        )
    )
    part = task_to_v1(task).to_wire()["status"]["message"]["parts"][0]
    assert part == {"text": "hi", "metadata": {"k": "v"}}


def test_a_part_with_an_unknown_key_sends_its_artifact_whole() -> None:
    artifact = {"artifactId": "a1", "parts": [{"kind": "text", "text": "x", "lang": "en"}]}
    art = task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]
    assert art["parts"] == [{"data": artifact}]


@pytest.mark.parametrize(
    "part",
    [
        {"kind": "text", "text": "x", "data": {"n": 1}},  # a stray data key on a text part
        {"kind": "data", "data": {"n": 1}, "text": "x"},  # a stray text key on a data part
        {"kind": "data"},  # a data part without its data
        {"kind": "text", "text": 3},  # text that is not a string
    ],
)
def test_parts_that_would_lose_a_key_send_their_artifact_whole(part) -> None:
    artifact = {"artifactId": "a1", "parts": [part]}
    art = task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]
    assert art["parts"] == [{"data": artifact}]


def test_explicit_null_data_survives_when_nested() -> None:
    from cabal.core.a2a.v1 import ArtifactV1

    msg = MessageV1(message_id="m", role="ROLE_AGENT", parts=[PartV1(data=None)])
    assert msg.to_wire()["parts"] == [{"data": None}]
    art = ArtifactV1(artifact_id="a", parts=[PartV1(data=None)])
    assert art.to_wire()["parts"] == [{"data": None}]
    artifact = {"artifactId": "a1", "parts": [{"kind": "data", "data": None}]}
    assert task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]["parts"] == [
        {"data": None}
    ]


@pytest.mark.parametrize(
    "part, expected",
    [
        ({"text": "x"}, {"text": "x"}),  # already a v1.0 Part
        (
            {"url": "https://x/f", "mediaType": "image/png"},
            {"url": "https://x/f", "mediaType": "image/png"},
        ),
        ({"text": "x", "data": 1}, {"data": {"text": "x", "data": 1}}),  # two content fields
        ({"note": "free"}, {"data": {"note": "free"}}),  # not a Part shape
    ],
)
def test_kindless_parts(part, expected) -> None:
    artifact = {"artifactId": "a1", "parts": [part]}
    assert task_to_v1(_task(artifacts=[artifact])).to_wire()["artifacts"][0]["parts"] == [expected]
