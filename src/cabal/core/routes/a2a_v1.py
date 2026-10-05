"""The A2A v1.0 JSON-RPC endpoint for each persona, `/agents/{name}/a2a` (epic #272).

A request passes four checks, in this order, before a method runs: the body
parses (-32700), it is a JSON-RPC 2.0 request (-32600), it asks for
`A2A-Version: 1.0` (-32009, #265), and it carries a bearer token (HTTP 401
with `WWW-Authenticate: Bearer`). Then:

- `SendMessage` dispatches a new task to the persona named in the path, with
  the same token policy as the legacy `message/send`
  (`_dispatch_auth.dispatch_token`), and returns the finished task.
- `GetTask` returns a task the caller may see. A task that does not exist,
  belongs to another caller, or belongs to another persona gets the same
  TaskNotFoundError (-32001), so the answer never reveals that a task exists
  (spec 3.3.2).
- `SendStreamingMessage` and `SubscribeToTask` return UnsupportedOperationError
  (-32004): streaming is off in the first cut (#275).
- Every other method returns MethodNotFound (-32601) for now (#268).

A request's `tenant`, when sent, must name the persona in the path. The path
is distinct from the legacy `POST /agents/{name}` and from the `/a2a` prefix
of `POST /a2a/tasks`; a trailing slash is accepted, because clients such as
the A2A compatibility kit post to the interface URL plus "/".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select

from cabal.core.a2a.types import Task, TaskStatus
from cabal.core.a2a.v1 import (
    GetTaskRequestV1,
    SendMessageRequestV1,
    SendMessageResponseV1,
    UnsupportedPartError,
    conversation_to_v1,
    message_from_v1,
    task_to_v1,
)
from cabal.core.a2a.v1_errors import (
    CONTENT_TYPE_NOT_SUPPORTED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSON_PARSE_ERROR,
    METHOD_NOT_FOUND,
    PUSH_NOTIFICATION_NOT_SUPPORTED,
    TASK_NOT_FOUND,
    UNSUPPORTED_OPERATION,
    VERSION_NOT_SUPPORTED,
    a2a_error,
    error_response,
    forbidden,
    jsonrpc_error,
    unauthenticated,
)
from cabal.core.a2a.version import is_v1, requested_version
from cabal.core.broker.router import (
    AgentNotFoundError,
    CallerJWTError,
    ConfigurationError,
    DispatchError,
    GuardrailBlocked,
)
from cabal.core.db import open_session
from cabal.core.models import Conversation
from cabal.core.routes._dispatch_auth import DispatchRefusal, dispatch_token, verify_caller

router = APIRouter(prefix="/agents", tags=["a2a-v1"])

_STREAMING_METHODS = frozenset({"SendStreamingMessage", "SubscribeToTask"})


def _reply(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, media_type="application/json")


def _result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return _reply({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, error: dict[str, Any]) -> JSONResponse:
    return _reply(error_response(request_id, error))


def _valid_id(value: Any) -> bool:
    """JSON-RPC 2.0: an id is a string, a number or null (a bool is not a number)."""
    return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))


def _is_jsonrpc_request(body: Any) -> bool:
    """A JSON-RPC 2.0 request object: `"jsonrpc": "2.0"`, a string `method`,
    a valid `id` when present, and `params` an object or array when present
    (JSON-RPC 2.0 lets `params` be omitted, not null)."""
    return (
        isinstance(body, dict)
        and body.get("jsonrpc") == "2.0"
        and isinstance(body.get("method"), str)
        and _valid_id(body.get("id"))
        and ("params" not in body or isinstance(body["params"], (dict, list)))
    )


def _bearer(authorization: str | None) -> str:
    """The bearer token, or HTTP 401 with a Bearer challenge."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    raise unauthenticated()


def _refuse(refusal: DispatchRefusal, request_id: Any) -> JSONResponse:
    """A dispatch refusal as v1.0 answers it: HTTP 401 or 403, or -32603."""
    if refusal.kind == "unauthenticated":
        raise unauthenticated(refusal.message)
    if refusal.kind == "forbidden":
        raise forbidden(refusal.message)
    return _error(request_id, jsonrpc_error(INTERNAL_ERROR, refusal.message))


def _params_error(request_id: Any, message: str) -> JSONResponse:
    return _error(request_id, jsonrpc_error(INVALID_PARAMS, message))


def _tenant_mismatch(tenant: str | None, agent_name: str) -> bool:
    return tenant is not None and tenant != "" and tenant != agent_name


@router.post("/{agent_name}/a2a")
@router.post("/{agent_name}/a2a/", include_in_schema=False)
async def v1_jsonrpc(
    request: Request,
    agent_name: str = Path(..., min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any undecodable body is a parse error
        return _error(None, jsonrpc_error(JSON_PARSE_ERROR, "Invalid JSON payload"))
    request_id = body.get("id") if isinstance(body, dict) else None
    if not _valid_id(request_id):
        request_id = None  # never echo back an id that JSON-RPC does not allow

    # Same order as the legacy routes: parse, then the envelope, then the version.
    if not _is_jsonrpc_request(body):
        return _error(request_id, jsonrpc_error(INVALID_REQUEST, "Request payload validation error"))
    version = requested_version(request)
    if not is_v1(version):
        return _error(
            request_id,
            a2a_error(
                VERSION_NOT_SUPPORTED,
                message=f"this interface serves A2A 1.0, not {version or '0.3 (no A2A-Version)'}",
                metadata={"requestedVersion": version or "", "supportedVersion": "1.0"},
            ),
        )
    raw_token = _bearer(authorization)

    method = body["method"]
    params = body.get("params", {})
    if method == "SendMessage":
        return await _send_message(request, agent_name, raw_token, request_id, params)
    if method == "GetTask":
        return await _get_task(request, agent_name, raw_token, request_id, params)
    if method in _STREAMING_METHODS:
        return _error(
            request_id,
            a2a_error(UNSUPPORTED_OPERATION, message=f"{method}: this agent does not stream"),
        )
    return _error(request_id, jsonrpc_error(METHOD_NOT_FOUND, f"method not found: {method!r}"))


async def _send_message(
    request: Request, agent_name: str, raw_token: str, request_id: Any, params: Any
) -> JSONResponse:
    if not isinstance(params, dict):
        return _params_error(request_id, "SendMessage params must be an object")
    try:
        req = SendMessageRequestV1.model_validate(params)
    except ValidationError as exc:
        return _params_error(request_id, f"invalid SendMessage params: {exc.error_count()} error(s)")
    if _tenant_mismatch(req.tenant, agent_name):
        return _params_error(request_id, f"tenant {req.tenant!r} does not match the path")
    cfg = req.configuration
    if cfg is not None and cfg.task_push_notification_config is not None:
        return _error(
            request_id,
            a2a_error(PUSH_NOTIFICATION_NOT_SUPPORTED, message="this agent does not send push notifications"),
        )
    if cfg is not None and cfg.return_immediately:
        return _error(
            request_id,
            a2a_error(UNSUPPORTED_OPERATION, message="returnImmediately is not supported yet (#277)"),
        )
    if req.message.task_id:
        return _error(
            request_id,
            a2a_error(UNSUPPORTED_OPERATION, message="continuing an existing task is not supported yet (#276)"),
        )
    try:
        message = message_from_v1(req.message)
    except UnsupportedPartError as exc:
        return _error(request_id, a2a_error(CONTENT_TYPE_NOT_SUPPORTED, message=str(exc)))

    token = await dispatch_token(request, raw_token, agent_name)
    if isinstance(token, DispatchRefusal):
        return _refuse(token, request_id)

    task = Task(
        **{
            "id": str(uuid.uuid4()),
            "contextId": req.message.context_id,
            "status": TaskStatus(state="submitted", message=message),
            "metadata": req.metadata or {},
        }
    )
    try:
        response = await request.app.state.dispatch_router.dispatch(
            target_agent=agent_name, target_generation=None, task=task, caller_jwt=token
        )
    except (AgentNotFoundError, ConfigurationError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CallerJWTError as exc:
        raise unauthenticated(str(exc)) from exc
    except GuardrailBlocked as exc:  # before DispatchError, its base class
        raise forbidden(str(exc)) from exc
    except DispatchError as exc:
        return _error(request_id, jsonrpc_error(INTERNAL_ERROR, str(exc)))

    v1_task = task_to_v1(
        response,
        timestamp=_now(),
        history_length=cfg.history_length if cfg is not None else None,
    )
    return _result(request_id, SendMessageResponseV1(task=v1_task).to_wire())


async def _get_task(
    request: Request, agent_name: str, raw_token: str, request_id: Any, params: Any
) -> JSONResponse:
    if not isinstance(params, dict):
        return _params_error(request_id, "GetTask params must be an object")
    try:
        req = GetTaskRequestV1.model_validate(params)
    except ValidationError as exc:
        return _params_error(request_id, f"invalid GetTask params: {exc.error_count()} error(s)")
    if _tenant_mismatch(req.tenant, agent_name):
        return _params_error(request_id, f"tenant {req.tenant!r} does not match the path")

    claims = verify_caller(request, raw_token)
    if isinstance(claims, DispatchRefusal):
        return _refuse(claims, request_id)
    is_operator = "operator" in (claims.get("cabal_scope") or [])

    async with open_session(request.app.state.sessionmaker) as session:
        row = (
            await session.execute(select(Conversation).where(Conversation.task_id == req.id))
        ).scalar_one_or_none()
    visible = (
        row is not None
        and row.target_agent == agent_name
        and (is_operator or claims.get("sub") == row.caller_agent)
    )
    if not visible or row is None:
        # One answer for missing, another caller's, and another persona's task.
        return _error(request_id, a2a_error(TASK_NOT_FOUND))
    v1_task = conversation_to_v1(
        task_id=row.task_id,
        state=row.state,
        result_json=row.result_json,
        updated_at=row.updated_at,
        history_length=req.history_length,
    )
    return _result(request_id, v1_task.to_wire())


def _now() -> datetime:
    """The status timestamp for a finished SendMessage task."""
    return datetime.now(UTC)
