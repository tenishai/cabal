"""A2A v1.0 errors for the v1.0 JSON-RPC interface (#269).

The legacy routes use their own codes in `cabal.core.a2a.jsonrpc`
(`ERROR_UNAUTHORIZED = -32001`, `ERROR_FORBIDDEN = -32002`,
`ERROR_AGENT_NOT_FOUND = -32003`). In A2A v1.0 those numbers are
TaskNotFoundError, TaskNotCancelableError and PushNotificationNotSupportedError
(spec 5.4), so the v1.0 interface (#267) will use this module instead:

- **A2A errors** (`a2a_error`) carry their v1.0 code and, as spec 9.5
  requires, a `google.rpc.ErrorInfo` in the error's `data` array.
- **Authentication and authorization** helpers (`unauthenticated`,
  `forbidden`) give HTTP 401 with `WWW-Authenticate: Bearer` for a missing or
  invalid token, and HTTP 403 for an authenticated caller who is not allowed.
  Spec 3.3.2 allows HTTP status for auth, and answering auth that way keeps it
  out of the JSON-RPC code space, where A2A owns -32001 to -32099.
- **Standard errors** (`jsonrpc_error`) use the JSON-RPC codes; `-32602` is
  the one for invalid params, such as a `tenant` that does not match the path.

The legacy codes stay on the legacy routes, which existing cabals depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
ERROR_INFO_DOMAIN = "a2a-protocol.org"

# Standard JSON-RPC 2.0 codes, which the v1.0 interface will use as they are.
JSON_PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass(frozen=True, slots=True)
class A2AError:
    """One A2A v1.0 error type: its JSON-RPC code and its ErrorInfo reason."""

    name: str
    code: int
    message: str

    @property
    def reason(self) -> str:
        """The type name in UPPER_SNAKE_CASE without the `Error` suffix (spec 9.5)."""
        stem = self.name.removesuffix("Error")
        out = [stem[0]]
        for prev, ch in zip(stem, stem[1:], strict=False):
            if ch.isupper() and not prev.isupper():
                out.append("_")
            out.append(ch)
        return "".join(out).upper()


# Spec 5.4, in code order.
TASK_NOT_FOUND = A2AError("TaskNotFoundError", -32001, "Task not found")
TASK_NOT_CANCELABLE = A2AError("TaskNotCancelableError", -32002, "Task not cancelable")
PUSH_NOTIFICATION_NOT_SUPPORTED = A2AError(
    "PushNotificationNotSupportedError", -32003, "Push notification not supported"
)
UNSUPPORTED_OPERATION = A2AError("UnsupportedOperationError", -32004, "Unsupported operation")
CONTENT_TYPE_NOT_SUPPORTED = A2AError(
    "ContentTypeNotSupportedError", -32005, "Content type not supported"
)
INVALID_AGENT_RESPONSE = A2AError("InvalidAgentResponseError", -32006, "Invalid agent response")
EXTENDED_AGENT_CARD_NOT_CONFIGURED = A2AError(
    "ExtendedAgentCardNotConfiguredError", -32007, "Extended agent card not configured"
)
EXTENSION_SUPPORT_REQUIRED = A2AError(
    "ExtensionSupportRequiredError", -32008, "Extension support required"
)
VERSION_NOT_SUPPORTED = A2AError("VersionNotSupportedError", -32009, "Version not supported")

A2A_ERRORS: tuple[A2AError, ...] = (
    TASK_NOT_FOUND,
    TASK_NOT_CANCELABLE,
    PUSH_NOTIFICATION_NOT_SUPPORTED,
    UNSUPPORTED_OPERATION,
    CONTENT_TYPE_NOT_SUPPORTED,
    INVALID_AGENT_RESPONSE,
    EXTENDED_AGENT_CARD_NOT_CONFIGURED,
    EXTENSION_SUPPORT_REQUIRED,
    VERSION_NOT_SUPPORTED,
)


def a2a_error(
    error: A2AError, *, message: str | None = None, metadata: dict[str, str] | None = None
) -> dict[str, Any]:
    """The JSON-RPC `error` object for an A2A error, with its ErrorInfo.

    `metadata` is ErrorInfo's string map (for example `{"taskId": ...}`).
    Keep it to what the caller may know: an inaccessible task is reported as
    TaskNotFoundError with nothing that reveals it exists (spec 3.3.2).
    """
    info: dict[str, Any] = {
        "@type": ERROR_INFO_TYPE,
        "reason": error.reason,
        "domain": ERROR_INFO_DOMAIN,
    }
    if metadata:
        info["metadata"] = {k: str(v) for k, v in metadata.items()}
    return {"code": error.code, "message": message or error.message, "data": [info]}


def jsonrpc_error(code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    """The JSON-RPC `error` object for a standard (non-A2A) error."""
    out: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        out["data"] = data
    return out


def error_response(request_id: str | int | None, error: dict[str, Any]) -> dict[str, Any]:
    """A complete JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def unauthenticated(detail: str = "a valid bearer token is required") -> HTTPException:
    """HTTP 401 with a Bearer challenge, for a missing or invalid token."""
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def forbidden(detail: str = "the caller is not allowed to call this agent") -> HTTPException:
    """HTTP 403, for an authenticated caller the switchboard refuses."""
    return HTTPException(status_code=403, detail=detail)


__all__ = [
    "A2A_ERRORS",
    "CONTENT_TYPE_NOT_SUPPORTED",
    "ERROR_INFO_DOMAIN",
    "ERROR_INFO_TYPE",
    "EXTENDED_AGENT_CARD_NOT_CONFIGURED",
    "EXTENSION_SUPPORT_REQUIRED",
    "INTERNAL_ERROR",
    "INVALID_AGENT_RESPONSE",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSON_PARSE_ERROR",
    "METHOD_NOT_FOUND",
    "PUSH_NOTIFICATION_NOT_SUPPORTED",
    "TASK_NOT_CANCELABLE",
    "TASK_NOT_FOUND",
    "UNSUPPORTED_OPERATION",
    "VERSION_NOT_SUPPORTED",
    "A2AError",
    "a2a_error",
    "error_response",
    "forbidden",
    "jsonrpc_error",
    "unauthenticated",
]
