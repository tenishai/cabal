"""A2A v1.0 errors (#269), checked against spec 5.4 and 9.5 (A2A commit 1736957)."""

from __future__ import annotations

import pytest

from cabal.core.a2a import jsonrpc as legacy
from cabal.core.a2a.v1_errors import (
    A2A_ERRORS,
    INVALID_PARAMS,
    TASK_NOT_FOUND,
    VERSION_NOT_SUPPORTED,
    a2a_error,
    error_response,
    forbidden,
    jsonrpc_error,
    unauthenticated,
)

# Spec 5.4, Error Code Mappings: name -> JSON-RPC code.
SPEC_5_4 = {
    "TaskNotFoundError": -32001,
    "TaskNotCancelableError": -32002,
    "PushNotificationNotSupportedError": -32003,
    "UnsupportedOperationError": -32004,
    "ContentTypeNotSupportedError": -32005,
    "InvalidAgentResponseError": -32006,
    "ExtendedAgentCardNotConfiguredError": -32007,
    "ExtensionSupportRequiredError": -32008,
    "VersionNotSupportedError": -32009,
}


def test_every_a2a_error_has_its_spec_code() -> None:
    assert {e.name: e.code for e in A2A_ERRORS} == SPEC_5_4


@pytest.mark.parametrize(
    "name, reason",
    [
        ("TaskNotFoundError", "TASK_NOT_FOUND"),
        ("TaskNotCancelableError", "TASK_NOT_CANCELABLE"),
        ("PushNotificationNotSupportedError", "PUSH_NOTIFICATION_NOT_SUPPORTED"),
        ("UnsupportedOperationError", "UNSUPPORTED_OPERATION"),
        ("ContentTypeNotSupportedError", "CONTENT_TYPE_NOT_SUPPORTED"),
        ("InvalidAgentResponseError", "INVALID_AGENT_RESPONSE"),
        ("ExtendedAgentCardNotConfiguredError", "EXTENDED_AGENT_CARD_NOT_CONFIGURED"),
        ("ExtensionSupportRequiredError", "EXTENSION_SUPPORT_REQUIRED"),
        ("VersionNotSupportedError", "VERSION_NOT_SUPPORTED"),
    ],
)
def test_reason_is_upper_snake_without_error(name: str, reason: str) -> None:
    (error,) = [e for e in A2A_ERRORS if e.name == name]
    assert error.reason == reason


def test_a2a_error_matches_the_spec_example() -> None:
    """Spec 9.5's example A2A-specific error response."""
    body = error_response(
        2,
        a2a_error(
            TASK_NOT_FOUND,
            metadata={"taskId": "nonexistent-task-id", "timestamp": "2025-11-09T10:30:00.000Z"},
        ),
    )
    assert body == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": -32001,
            "message": "Task not found",
            "data": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "TASK_NOT_FOUND",
                    "domain": "a2a-protocol.org",
                    "metadata": {
                        "taskId": "nonexistent-task-id",
                        "timestamp": "2025-11-09T10:30:00.000Z",
                    },
                }
            ],
        },
    }


def test_error_info_metadata_is_optional_and_stringified() -> None:
    assert "metadata" not in a2a_error(VERSION_NOT_SUPPORTED)["data"][0]
    assert a2a_error(TASK_NOT_FOUND, metadata={"n": 3})["data"][0]["metadata"] == {"n": "3"}


def test_every_a2a_error_carries_error_info() -> None:
    for error in A2A_ERRORS:
        body = a2a_error(error)
        assert body["code"] == error.code
        (info,) = body["data"]
        assert info["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
        assert info["domain"] == "a2a-protocol.org"
        assert info["reason"] == error.reason


def test_standard_errors_use_plain_data() -> None:
    assert jsonrpc_error(INVALID_PARAMS, "tenant does not match the path") == {
        "code": -32602,
        "message": "tenant does not match the path",
    }
    assert jsonrpc_error(-32601, "Method not found", {"method": "x"})["data"] == {"method": "x"}


def test_auth_is_http_with_a_bearer_challenge() -> None:
    exc = unauthenticated()
    assert exc.status_code == 401
    assert exc.headers == {"WWW-Authenticate": "Bearer"}
    assert forbidden().status_code == 403


def test_legacy_auth_codes_collide_with_a2a_codes() -> None:
    """Why this module exists: the legacy routes' auth and agent-not-found codes
    are numbers A2A v1.0 assigns to other errors, so the v1.0 interface cannot
    use them. It answers auth with HTTP status instead (see the test above)."""
    a2a_codes = {e.code for e in A2A_ERRORS}
    assert {legacy.ERROR_UNAUTHORIZED, legacy.ERROR_FORBIDDEN, legacy.ERROR_AGENT_NOT_FOUND} <= (
        a2a_codes
    )
    assert all(-32099 <= c <= -32001 for c in a2a_codes)


def test_the_a2a_error_set_holds_only_spec_errors() -> None:
    """No auth or cabal-specific error is slipped into the A2A set, where it
    would claim a code from A2A's range."""
    assert {e.name for e in A2A_ERRORS} == set(SPEC_5_4)
    assert len({e.code for e in A2A_ERRORS}) == len(A2A_ERRORS)
