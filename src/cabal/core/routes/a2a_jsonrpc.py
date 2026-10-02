"""A2A v1.0 JSON-RPC route mounted per-agent at /agents/{name}.

Each agent exposes:
    GET  /agents/{name}/.well-known/agent-card  → signed AgentCard JWS
    POST /agents/{name}                          → JSON-RPC 2.0 envelope

Methods:
    message/send  → dispatch a new Task to {name}, return the completed Task
    tasks/get     → fetch the Task body from conversations.result_json
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from cabal.core import dispatch_pairs
from cabal.core.a2a.jsonrpc import (
    ERROR_AGENT_NOT_FOUND,
    ERROR_FORBIDDEN,
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
from cabal.core.auth.revocations import refusal
from cabal.core.broker.router import (
    AgentNotFoundError,
    CallerJWTError,
    ConfigurationError,
    DispatchError,
    GuardrailBlocked,
)
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
    if (why := refusal(getattr(request.app.state, "revocations", None), claims)) is not None:
        return None, make_error_response(id=None, code=ERROR_UNAUTHORIZED, message=why)

    if claims.get("aud") == target:
        return raw_token, None

    # Mirrors routes/tasks._resolve_dispatch_token. JSON-RPC is the A2A surface
    # an external agent actually dials, so the two gates must agree on who may
    # dispatch; a colleague accepted over REST and refused here is the same
    # token getting two answers.
    scope = claims.get("cabal_scope") or []
    if not ({"operator", "colleague"} & set(scope)):
        return None, make_error_response(
            id=None,
            code=ERROR_UNAUTHORIZED,
            message=(
                f"token aud={claims.get('aud')!r} cannot dispatch to {target!r} "
                "(requires operator or colleague scope)"
            ),
        )

    sub = claims.get("sub") or ""
    # A token with no sub failed to identify its bearer (cabal#184). Refuse to
    # re-mint it rather than default the principal to "operator".
    if not sub:
        return None, make_error_response(
            id=None,
            code=ERROR_UNAUTHORIZED,
            message="token carries no sub; refusing to re-mint a dispatch token",
        )

    sessionmaker = request.app.state.sessionmaker
    master_key = request.app.state.master_key
    async with open_session(sessionmaker) as session:
        # THE PAIRS POLICY HAS TO LIVE HERE TOO, and its absence was a full
        # bypass: the same colleague token refused by POST /a2a/tasks was
        # dispatching through POST /agents/{name} because this twin re-minted
        # without ever consulting the table. Scope alone is not the gate —
        # scope says who may ask for a re-mint, the pairs table says who may
        # call whom, and a policy enforced on one of two doors is not enforced.
        if "operator" not in scope:
            if not await dispatch_pairs.is_allowed(session, caller=sub, target=target):
                return None, make_error_response(
                    id=None,
                    code=ERROR_FORBIDDEN,
                    message=f"{sub} is not a colleague of {target} yet. Ask in the room.",
                )
        stored = await load_active_signing_key(session, master_key=master_key)
    if stored is None:
        return None, make_error_response(
            id=None, code=ERROR_INTERNAL, message="no active signing key"
        )
    return (
        mint(
            signing_key=stored,
            sub=sub,
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
        raise HTTPException(status_code=404, detail=f"agent {agent_name!r} not found")
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
    if rpc_req.method == "message/stream":
        # SSE streaming response — handled by a separate endpoint that
        # returns text/event-stream rather than JSON.
        return make_error_response(
            id=rpc_req.id,
            code=ERROR_INVALID_REQUEST,
            message=(
                "message/stream must be sent to "
                f"/agents/{agent_name}/stream with the same params; "
                "the JSON-RPC POST endpoint returns single responses only"
            ),
        )
    return make_error_response(
        id=rpc_req.id,
        code=ERROR_METHOD_NOT_FOUND,
        message=f"unknown method: {rpc_req.method!r}",
    )


@router.post("/{agent_name}/stream")
async def per_agent_stream(
    request: Request,
    agent_name: str = Path(..., min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """A2A v1.0 message/stream endpoint.

    Body is the same JSON-RPC envelope used by /agents/{name}, but the
    response is an SSE stream: each text delta from the LLM becomes an
    `event: chunk` frame; a final `event: status-update` frame carries
    the completed Task.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    try:
        rpc_req = JSONRPCRequest.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        return StreamingResponse(
            _sse_error_only(
                ERROR_INVALID_REQUEST, f"invalid JSON-RPC envelope: {exc}"
            ),
            media_type="text/event-stream",
        )

    raw_token = _bearer(authorization)
    if raw_token is None:
        return StreamingResponse(
            _sse_error_only(ERROR_UNAUTHORIZED, "missing Authorization header"),
            media_type="text/event-stream",
        )

    if rpc_req.method != "message/stream":
        return StreamingResponse(
            _sse_error_only(
                ERROR_METHOD_NOT_FOUND,
                f"this endpoint only handles message/stream, not {rpc_req.method!r}",
            ),
            media_type="text/event-stream",
        )

    if rpc_req.params is None:
        return StreamingResponse(
            _sse_error_only(ERROR_INVALID_PARAMS, "params required"),
            media_type="text/event-stream",
        )
    try:
        params = MessageSendParams.model_validate(rpc_req.params)
    except Exception as exc:  # noqa: BLE001
        return StreamingResponse(
            _sse_error_only(ERROR_INVALID_PARAMS, f"invalid params: {exc}"),
            media_type="text/event-stream",
        )

    dispatch_token, err = await _resolve_dispatch_token(request, raw_token, agent_name)
    if err is not None:
        return StreamingResponse(
            _sse_error_only(err.error.code, err.error.message),
            media_type="text/event-stream",
        )

    from cabal.core.a2a.types import Task, TaskStatus

    task_id = str(uuid.uuid4())
    # cabal#59 — propagate caller-supplied contextId so the DoomLoopGuardrail
    # can group re-delegations by a stable session key across A2A hops.
    task = Task(
        **{
            "id": task_id,
            "contextId": params.context_id,
            "status": TaskStatus(state="submitted", message=params.message),
            "metadata": params.metadata or {},
        }
    )

    return StreamingResponse(
        _stream_task_events(
            request.app.state.dispatch_router,
            task,
            agent_name,
            dispatch_token,
            rpc_req.id,
        ),
        media_type="text/event-stream",
    )


async def _stream_task_events(
    router_: Any,
    task: Any,
    agent_name: str,
    dispatch_token: str,
    request_id: Any,
) -> AsyncIterator[bytes]:
    """SSE frame generator for message/stream.

    Delegates to router.dispatch_streaming() so all §6.12 invariants
    (JWT re-verification, bookkeeping, hooks) are enforced by the same
    path as non-streaming dispatch.
    """
    full_text = ""
    try:
        async for delta in router_.dispatch_streaming(
            target_agent=agent_name,
            target_generation=None,
            task=task,
            caller_jwt=dispatch_token,
        ):
            full_text += delta
            yield _sse_event(
                "chunk", {"jsonrpc": "2.0", "id": request_id, "delta": delta}
            )
    except (AgentNotFoundError, ConfigurationError) as exc:
        yield _sse_event("error", {"jsonrpc": "2.0", "id": request_id, "error": {"code": ERROR_AGENT_NOT_FOUND, "message": str(exc), "partial_text": full_text}})
        return
    except CallerJWTError as exc:
        yield _sse_event("error", {"jsonrpc": "2.0", "id": request_id, "error": {"code": ERROR_UNAUTHORIZED, "message": str(exc), "partial_text": full_text}})
        return
    # BEFORE the bare DispatchError arm below, which reports ERROR_INTERNAL.
    # -32603 says "server fault" and the conventional response to one is to
    # retry -- so reporting a guardrail refusal that way sustains the very
    # loop the guardrail exists to break (#153 review). FORBIDDEN says the
    # refusal is about the caller and will not change on retry.
    except GuardrailBlocked as exc:
        yield _sse_event("error", {"jsonrpc": "2.0", "id": request_id, "error": {"code": ERROR_FORBIDDEN, "message": str(exc), "partial_text": full_text}})
        return
    except DispatchError as exc:
        yield _sse_event(
            "error",
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": ERROR_INTERNAL,
                    "message": str(exc),
                    "partial_text": full_text,
                },
            },
        )
        return
    except Exception as exc:  # noqa: BLE001
        yield _sse_event(
            "error",
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": ERROR_INTERNAL,
                    "message": f"stream failed: {exc}",
                    "partial_text": full_text,
                },
            },
        )
        return

    # The router has already written bookkeeping + fired hooks; emit the
    # final SSE frame so the client gets a well-formed completion event.
    from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart

    final_task = Task(
        id=task.id,
        status=TaskStatus(
            state="completed",
            message=Message(role="agent", parts=[TextPart(kind="text", text=full_text)]),
        ),
        metadata=task.metadata,
    )
    yield _sse_event(
        "status-update",
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": final_task.model_dump(by_alias=True, exclude_none=True),
            "final": True,
        },
    )


def _sse_event(event_name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode()


async def _sse_error_only(code: int, message: str) -> AsyncIterator[bytes]:
    yield _sse_event(
        "error",
        {"jsonrpc": "2.0", "error": {"code": code, "message": message}},
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

    # cabal#59 — propagate caller-supplied contextId so the DoomLoopGuardrail
    # can group re-delegations by a stable session key across A2A hops.
    task = Task(
        **{
            "id": str(uuid.uuid4()),
            "contextId": params.context_id,
            "status": TaskStatus(state="submitted", message=params.message),
            "metadata": params.metadata or {},
        }
    )

    try:
        response = await request.app.state.dispatch_router.dispatch(
            target_agent=agent_name,
            target_generation=None,
            task=task,
            caller_jwt=dispatch_token,
        )
    except (AgentNotFoundError, ConfigurationError) as exc:
        return make_error_response(id=rpc_req.id, code=ERROR_AGENT_NOT_FOUND, message=str(exc))
    except CallerJWTError as exc:
        return make_error_response(id=rpc_req.id, code=ERROR_UNAUTHORIZED, message=str(exc))
    # BEFORE DispatchError, WHICH IS ITS BASE (cabal#152). A terminal guardrail
    # verdict is about the caller and will not change on retry, so ERROR_INTERNAL
    # -- the code a peer reads as "server fault, try again" -- is the one answer
    # that invites the storm this guard exists to end. The streaming path has
    # reported FORBIDDEN since #153; until now `message/send` did not, and a
    # comment in router.dispatch() claimed otherwise (#152 pre-pass, WRONG 1).
    except GuardrailBlocked as exc:
        return make_error_response(id=rpc_req.id, code=ERROR_FORBIDDEN, message=str(exc))
    except DispatchError as exc:
        return make_error_response(id=rpc_req.id, code=ERROR_INTERNAL, message=str(exc))

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

    try:
        claims = verify(
            raw_token,
            key_set=request.app.state.jwks_provider(),
            expected_audience=None,
        )
    except JWTError as exc:
        return make_error_response(
            id=rpc_req.id, code=ERROR_UNAUTHORIZED, message=str(exc)
        )
    if (why := refusal(getattr(request.app.state, "revocations", None), claims)) is not None:
        return make_error_response(id=rpc_req.id, code=ERROR_UNAUTHORIZED, message=why)

    caller_sub = claims.get("sub")
    is_operator = "operator" in (claims.get("cabal_scope") or [])

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
    # Owner scoping: caller must be the task owner or have operator scope.
    if not is_operator and caller_sub != row.caller_agent:
        return make_error_response(
            id=rpc_req.id, code=ERROR_UNAUTHORIZED, message="access denied"
        )
    if row.result_json is None:
        # Task is still in flight; return what we know
        return make_success_response(
            id=rpc_req.id,
            result={"id": row.task_id, "status": {"state": row.state}},
        )
    return make_success_response(id=rpc_req.id, result=row.result_json)
