"""A2A JSON-RPC 2.0 envelope types, using A2A's pre-1.0 method names (not A2A v1.0; see #261).

The A2A wire protocol is JSON-RPC 2.0 over HTTPS. This module defines the
pydantic models for parsing inbound requests and serializing responses.

Methods supported in Phase G:
    message/send  — create a new Task; returns Task
    tasks/get     — fetch a Task by id; returns Task

Phase H+ will add message/stream and tasks/resubscribe (SSE).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cabal.core.a2a.types import Message, Task

# Standard JSON-RPC 2.0 error codes
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603

# Cabal extensions (server-defined range)
ERROR_UNAUTHORIZED = -32001
ERROR_FORBIDDEN = -32002
ERROR_AGENT_NOT_FOUND = -32003
# cabal is draining before a restart (issue 405): retry after it restarts.
ERROR_DRAINING = -32004


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class MessageSendParams(_Open):
    """A2A message/send params."""

    message: Message
    configuration: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    # cabal#59 — caller-supplied session key. When present the inbound Task
    # inherits it so the DoomLoopGuardrail can group re-delegations across
    # A2A hops by a stable session key rather than the per-hop uuid4 task id.
    context_id: str | None = Field(default=None, alias="contextId")


class TasksGetParams(_Open):
    id: str
    history_length: int | None = Field(default=None, alias="historyLength")


class JSONRPCRequest(_Open):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] | None = None


class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Any | None = None


class JSONRPCResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any | None = None
    error: JSONRPCError | None = None

    model_config = ConfigDict(extra="forbid")


def make_error_response(
    *,
    id: str | int | None,
    code: int,
    message: str,
    data: Any | None = None,
) -> JSONRPCResponse:
    return JSONRPCResponse(id=id, error=JSONRPCError(code=code, message=message, data=data))


def make_success_response(
    *,
    id: str | int | None,
    result: Any,
) -> JSONRPCResponse:
    return JSONRPCResponse(id=id, result=result)


__all__ = [
    "ERROR_AGENT_NOT_FOUND",
    "ERROR_FORBIDDEN",
    "ERROR_INTERNAL",
    "ERROR_INVALID_PARAMS",
    "ERROR_INVALID_REQUEST",
    "ERROR_METHOD_NOT_FOUND",
    "ERROR_PARSE",
    "ERROR_UNAUTHORIZED",
    "JSONRPCError",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "MessageSendParams",
    "TasksGetParams",
    "make_error_response",
    "make_success_response",
    "Task",
]
