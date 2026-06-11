"""A2A v1.0 JSON-RPC route mounted per-agent at /agents/{name}.

Each agent exposes:
    GET  /agents/{name}/.well-known/agent-card  → signed AgentCard JWS
    POST /agents/{name}                          → JSON-RPC 2.0 envelope

Methods:
    message/send  → dispatch a new Task to {name}, return the completed Task
    tasks/get     → fetch the Task body from conversations.result_json
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, Path, Request
from sqlalchemy import select

from cabal.core.a2a.jsonrpc import (
    ERROR_AGENT_NOT_FOUND,
    ERROR_INTERNAL,
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_METHOD_NOT_FOUND,
    ERROR_UNAUTHORIZED,
    JSONRPCRequest,
    JSONRPCResponse,
    MessageSendParams,
    TasksGetParams,
    make_error_response,
    make_success_response,
)
from cabal.core.a2a.types import Task, TaskStatus
from cabal.core.auth.jwt import JWTError, mint, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.broker.router import DispatchError
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Conversation

router = APIRouter(prefix="/agents", tags=["a2a"])


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return None


async def _resolve_dispatch_token(
    request: Request, raw_token: str, target: str
) -> tuple[str | None, JSONRPCResponse | None]:
    """Returns (dispatch_token, error_response). One is always None."""
    try:
        claims = verify(
            raw_token,
            key_set=request.app.state.jwks_provider(),
            expected_audience=None,
        )
    except JWTError as exc:
        return None, make_error_response(
            id=None, code=ERROR_UNAUTHORIZED, message=str(exc)
        )

    if claims.get("aud") == target:
        return raw_token, None

    scope = claims.get("cabal_scope") or []
    if "operator" not in scope:
        return None, make_error_response(
            id=None,
            code=ERROR_UNAUTHORIZED,
            message=f"token aud={claims.get('aud')!r} cannot dispatch to {target!r}",
        )

    sessionmaker = request.app.state.sessionmaker
    master_key = request.app.state.master_key
    async with open_session(sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=master_key)
    if stored is None:
        return None, make_error_response(
            id=None, code=ERROR_INTERNAL, message="no active signing key"
        )
    return (
        mint(
            signing_key=stored,
            sub=claims.get("sub", "operator"),
            aud=target,
            persona=claims.get("cabal_persona"),
            scope=["dispatch"],
        ),
        None,
    )


@router.get("/{agent_name}/.well-known/agent-card")
async def per_agent_card(
    request: Request,
    agent_name: str = Path(..., min_length=1, max_length=64),
) -> dict[str, Any]:
    """Return the agent's stored agent_card_json verbatim (jws + parsed)."""
    sessionmaker = request.app.state.sessionmaker
    async with open_session(sessionmaker) as session:
        row = await bulletin_store.get_active(session, agent_name)
    if row is None:
        return {"error": "agent not found", "name": agent_name}
    return row.agent_card_json


@router.post("/{agent_name}")
async def per_agent_jsonrpc(
    request: Request,
    agent_name: str = Path(..., min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> JSONRPCResponse:
    # Parse JSON-RPC envelope
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return make_error_response(
            id=None, code=ERROR_INVALID_REQUEST, message="request body is not JSON"
        )

    try:
        rpc_req = JSONRPCRequest.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        return make_error_response(
            id=body.get("id") if isinstance(body, dict) else None,
            code=ERROR_INVALID_REQUEST,
            message=f"invalid JSON-RPC envelope: {exc}",
        )

    raw_token = _bearer(authorization)
    if raw_token is None:
        return make_error_response(
            id=rpc_req.id,
            code=ERROR_UNAUTHORIZED,
            message="missing or malformed Authorization header",
        )

    if rpc_req.method == "message/send":
        return await _message_send(request, agent_name, raw_token, rpc_req)
    if rpc_req.method == "tasks/get":
        return await _tasks_get(request, raw_token, rpc_req)
    return make_error_response(
        id=rpc_req.id,
        code=ERROR_METHOD_NOT_FOUND,
        message=f"unknown method: {rpc_req.method!r}",
    )


async def _message_send(
    request: Request,
    agent_name: str,
    raw_token: str,
    rpc_req: JSONRPCRequest,
) -> JSONRPCResponse:
    if rpc_req.params is None:
        return make_error_response(
            id=rpc_req.id, code=ERROR_INVALID_PARAMS, message="params required"
        )
    try:
        params = MessageSendParams.model_validate(rpc_req.params)
    except Exception as exc:  # noqa: BLE001
        return make_error_response(
            id=rpc_req.id,
            code=ERROR_INVALID_PARAMS,
            message=f"invalid params: {exc}",
        )

    dispatch_token, err = await _resolve_dispatch_token(request, raw_token, agent_name)
    if err is not None:
        err.id = rpc_req.id
        return err

    task = Task(
        id=str(uuid.uuid4()),
        status=TaskStatus(state="submitted", message=params.message),
        metadata=params.metadata or {},
    )

    try:
        response = await request.app.state.dispatch_router.dispatch(
            target_agent=agent_name,
            target_generation=None,
            task=task,
            caller_jwt=dispatch_token,
        )
    except DispatchError as exc:
        msg = str(exc)
        code = ERROR_INTERNAL
        if "no agent registered" in msg or "no handler" in msg:
            code = ERROR_AGENT_NOT_FOUND
        elif "caller JWT" in msg:
            code = ERROR_UNAUTHORIZED
        return make_error_response(id=rpc_req.id, code=code, message=msg)

    return make_success_response(
        id=rpc_req.id, result=response.model_dump(by_alias=True, exclude_none=True)
    )


async def _tasks_get(
    request: Request, raw_token: str, rpc_req: JSONRPCRequest
) -> JSONRPCResponse:
    if rpc_req.params is None:
        return make_error_response(
            id=rpc_req.id, code=ERROR_INVALID_PARAMS, message="params required"
        )
    try:
        params = TasksGetParams.model_validate(rpc_req.params)
    except Exception as exc:  # noqa: BLE001
        return make_error_response(
            id=rpc_req.id, code=ERROR_INVALID_PARAMS, message=f"invalid params: {exc}"
        )

    # tasks/get requires any valid token; we don't enforce a specific aud
    # because both the original caller and the operator can poll a task.
    try:
        verify(
            raw_token,
            key_set=request.app.state.jwks_provider(),
            expected_audience=None,
        )
    except JWTError as exc:
        return make_error_response(
            id=rpc_req.id, code=ERROR_UNAUTHORIZED, message=str(exc)
        )

    sessionmaker = request.app.state.sessionmaker
    async with open_session(sessionmaker) as session:
        result = await session.execute(
            select(Conversation).where(Conversation.task_id == params.id)
        )
        row = result.scalar_one_or_none()
    if row is None:
        return make_error_response(
            id=rpc_req.id, code=ERROR_AGENT_NOT_FOUND, message=f"task {params.id!r} not found"
        )
    if row.result_json is None:
        # Task is still in flight; return what we know
        return make_success_response(
            id=rpc_req.id,
            result={"id": row.task_id, "status": {"state": row.state}},
        )
    return make_success_response(id=rpc_req.id, result=row.result_json)
