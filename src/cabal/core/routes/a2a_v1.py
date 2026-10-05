"""The A2A v1.0 JSON-RPC endpoint for each persona, `/agents/{name}/a2a` (epic #272).

A request passes four checks, in this order, before a method runs: the body
parses (-32700), it is a JSON-RPC 2.0 request (-32600), it asks for
`A2A-Version: 1.0` (-32009, #265), and it carries a bearer token (HTTP 401
with `WWW-Authenticate: Bearer`). Then:

- `SendMessage` dispatches a new task to the persona named in the path, with
  the same token policy as the legacy `message/send`
  (`_dispatch_auth.dispatch_token`), and returns the task the persona returns.
- `GetTask` returns a task the caller may see. A task that does not exist,
  belongs to another caller, or belongs to another persona gets the same
  TaskNotFoundError (-32001), so the answer never reveals that a task exists
  (spec 3.3.2).
- `ListTasks` returns the caller's own tasks on this persona (every task on
  it, for the operator), newest status first, a page at a time (#268).
- `CancelTask` cancels a task running in this process through the
  `AbortCoordinator`, waits up to `CANCEL_WAIT_SECONDS` for its dispatch to
  record the end, and returns the task as it then stands. A finished task, one
  dispatched outside this process, and one with nothing running behind it get
  TaskNotCancelableError (-32002); a task the caller may not see gets
  TaskNotFoundError (#268).
- `SendStreamingMessage` and `SubscribeToTask` return UnsupportedOperationError
  (-32004): streaming is off in the first cut (#275). `GetExtendedAgentCard`
  returns it too, and the four push notification config methods return
  PushNotificationNotSupportedError (-32003): the cards declare neither (#270).
- Every other method returns MethodNotFound (-32601).

A request's `tenant`, when sent, must name the persona in the path. The path
is distinct from the legacy `POST /agents/{name}` and from the `/a2a` prefix
of `POST /a2a/tasks`; a trailing slash is accepted, because clients such as
the A2A compatibility kit post to the interface URL plus "/".
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select

from cabal.config.identity import CabalConfig, resolve_public_url
from cabal.core.a2a.types import Message, Task, TaskStatus
from cabal.core.a2a.v1 import (
    STATE_FROM_V1,
    CancelTaskRequestV1,
    GetTaskRequestV1,
    ListTasksRequestV1,
    ListTasksResponseV1,
    SendMessageRequestV1,
    SendMessageResponseV1,
    UnsupportedPartError,
    conversation_to_v1,
    message_from_v1,
    task_to_v1,
)
from cabal.core.a2a.v1_card import INPUT_MODES, part_media_type, persona_card, signed_card
from cabal.core.a2a.v1_errors import (
    CONTENT_TYPE_NOT_SUPPORTED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSON_PARSE_ERROR,
    METHOD_NOT_FOUND,
    PUSH_NOTIFICATION_NOT_SUPPORTED,
    TASK_NOT_CANCELABLE,
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
from cabal.core.auth.keys import load_active_signing_key
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
from cabal.core.routes._dispatch_auth import DispatchRefusal, dispatch_token, verify_caller

router = APIRouter(prefix="/agents", tags=["a2a-v1"])

_STREAMING_METHODS = frozenset({"SendStreamingMessage", "SubscribeToTask"})
_PUSH_CONFIG_METHODS = frozenset(
    {
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
    }
)
# States in which a task's dispatch has ended; CancelTask refuses them. (An
# operator can still resume a canceled task through /admin when its persona is
# local and left a checkpoint.)
_TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
# How long CancelTask waits for the canceled dispatch to record its end.
# Cancellation is cooperative, so a running persona stops at its next safe
# boundary; past this, CancelTask returns the task as it stands.
CANCEL_WAIT_SECONDS = 5.0
_CANCEL_POLL_SECONDS = 0.05
_DEFAULT_PAGE_SIZE = 50


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


async def signed_agent_card(
    request: Request, agent_name: str, *, front_door: bool = False
) -> dict[str, Any]:
    """The signed v1.0 card for the bulletin agent `agent_name` (#270).

    503 when `cabal.public_url` is unset or malformed: the card's interface
    URL must be the address a client reaches, and nothing else knows it. An
    unknown agent, a federated peer (its card is the peer's own) and a
    presence row have no card: 404, or 503 for the front door, where it means
    `cabal.front_door` names the wrong agent. The front door's description
    also points at the directory, inside the signed content.
    """
    try:
        base_url = resolve_public_url(getattr(request.app.state, "identity", None))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if base_url is None:
        raise HTTPException(
            status_code=503,
            detail="cabal.public_url is not set, and an A2A v1.0 card is not served without it",
        )
    async with open_session(request.app.state.sessionmaker) as session:
        row = await bulletin_store.get_active(session, agent_name)
        stored = await load_active_signing_key(session, master_key=request.app.state.master_key)
    if row is None or row.kind in {"federated", "session"}:
        if front_door:
            raise HTTPException(
                status_code=503,
                detail=f"cabal.front_door names {agent_name!r}, which has no A2A v1.0 card here",
            )
        raise HTTPException(status_code=404, detail=f"agent {agent_name!r} not found")
    if stored is None:
        raise HTTPException(status_code=500, detail="no active signing key")
    legacy = row.agent_card_json or {}
    card = persona_card(
        name=row.name,
        persona=row.persona_name,
        legacy=legacy.get("card", legacy) if isinstance(legacy, dict) else {},
        base_url=base_url,
    )
    if front_door:
        pointer = f"Other agents on this cabal are listed at {base_url}/directory."
        card.description = f"{card.description} {pointer}" if card.description else pointer
    return signed_card(card, stored)


@router.get("/{agent_name}/.well-known/agent-card.json")
async def v1_agent_card(
    request: Request, agent_name: str = Path(..., min_length=1, max_length=64)
) -> JSONResponse:
    """The agent's A2A v1.0 card, signed (#270). The legacy card stays at
    `/agents/{name}/.well-known/agent-card`."""
    return JSONResponse(await signed_agent_card(request, agent_name), media_type="application/json")


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
    if method == "ListTasks":
        return await _list_tasks(request, agent_name, raw_token, request_id, params)
    if method == "CancelTask":
        return await _cancel_task(request, agent_name, raw_token, request_id, params)
    if method in _STREAMING_METHODS:
        return _error(
            request_id,
            a2a_error(UNSUPPORTED_OPERATION, message=f"{method}: this agent does not stream"),
        )
    if method in _PUSH_CONFIG_METHODS:
        return _error(
            request_id,
            a2a_error(PUSH_NOTIFICATION_NOT_SUPPORTED, message="this agent does not send push notifications"),
        )
    if method == "GetExtendedAgentCard":
        return _error(
            request_id,
            a2a_error(UNSUPPORTED_OPERATION, message="this agent has no extended agent card"),
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
        return await _continue_task(request, agent_name, raw_token, request_id, req)
    # The persona card declares what a message may carry (defaultInputModes,
    # #270); a part of any other media type is refused (CORE-SEND-003, #274).
    for part in req.message.parts:
        media_type = part_media_type(part)
        if media_type not in INPUT_MODES:
            return _error(
                request_id,
                a2a_error(
                    CONTENT_TYPE_NOT_SUPPORTED,
                    message=f"media type {media_type!r} is not accepted; send one of {list(INPUT_MODES)}",
                ),
            )
    try:
        message = message_from_v1(req.message)
    except UnsupportedPartError as exc:
        return _error(request_id, a2a_error(CONTENT_TYPE_NOT_SUPPORTED, message=str(exc)))

    token = await dispatch_token(request, raw_token, agent_name)
    if isinstance(token, DispatchRefusal):
        return _refuse(token, request_id)

    # The conversation key: the client's, or one generated here and returned
    # on the task (spec 3.4.1, CORE-MULTI-001a). A client-provided one also
    # brings back the conversation so far, as `_replay` reconstructs it (#276).
    history: list[Message] = []
    if req.message.context_id:
        context_id = req.message.context_id
        claims = verify_caller(request, raw_token)
        if not isinstance(claims, DispatchRefusal):
            history = await _replay(request, agent_name, claims.get("sub") or None, context_id)
    else:
        context_id = str(uuid.uuid4())
    task = Task(
        **{
            "id": str(uuid.uuid4()),
            "contextId": context_id,
            "status": TaskStatus(state="submitted", message=message),
            "history": history,
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


def _owner_filter(claims: dict[str, Any]) -> tuple[bool, str | None]:
    """(is the caller the operator, the caller's sub). A non-operator token
    with no sub owns nothing, so it never matches a row whose caller is NULL."""
    is_operator = "operator" in (claims.get("cabal_scope") or [])
    return is_operator, claims.get("sub") or None


def _visible(row: Conversation | None, agent_name: str, claims: dict[str, Any]) -> bool:
    """Whether the caller may see `row`: a task on this persona that the
    caller sent, or any task on it for the operator."""
    if row is None or row.target_agent != agent_name:
        return False
    is_operator, sub = _owner_filter(claims)
    return is_operator or (sub is not None and sub == row.caller_agent)


async def _load_row(request: Request, task_id: str) -> Conversation | None:
    async with open_session(request.app.state.sessionmaker) as session:
        return (
            await session.execute(select(Conversation).where(Conversation.task_id == task_id))
        ).scalar_one_or_none()


def _row_to_v1(row: Conversation, history_length: int | None = None) -> dict[str, Any]:
    return conversation_to_v1(
        task_id=row.task_id,
        state=row.state,
        result_json=row.result_json,
        updated_at=row.updated_at,
        history_length=history_length,
    ).to_wire()


async def _replay(
    request: Request, agent_name: str, sub: str | None, context_id: str
) -> list[Message]:
    """The conversation so far in `context_id`, for a new task's history.

    A local persona's reply carries the history it was given plus the
    incoming message, and the router stores a reply that is no longer than
    its input (a cancel, a guardrail block on the blocking path, a persona
    that drops history) with
    the input history plus that message. So the caller's latest task in the
    context on this persona with a stored result holds the conversation up to
    it (a remote peer's longer reply is stored as it came): its history, then its
    final status message (the reply, or the reason cabal ended the turn). A
    turn that raised stores no result and is not replayed. Only the caller's
    own tasks count, so one caller never replays another's conversation. The
    newest `cabal.context_replay_messages` messages are kept.
    """
    identity = getattr(request.app.state, "identity", None)
    limit = (identity.cabal if identity is not None else CabalConfig()).context_replay_messages
    if sub is None or limit == 0:
        return []
    async with open_session(request.app.state.sessionmaker) as session:
        row = (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.target_agent == agent_name,
                    Conversation.caller_agent == sub,
                    Conversation.result_json["contextId"].as_string() == context_id,
                )
                .order_by(_STATUS_TIME.desc(), Conversation.task_id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None or not row.result_json:
        return []
    earlier = Task.model_validate(row.result_json)
    turns = list(earlier.history)
    if earlier.status.message is not None:
        turns.append(earlier.status.message)
    return turns[-limit:]


async def _continue_task(
    request: Request, agent_name: str, raw_token: str, request_id: Any, req: SendMessageRequestV1
) -> JSONResponse:
    """A SendMessage that names a task (spec 3.4.3). The checks run now; a new
    turn on a paused task waits until a persona can pause one (#145)."""
    claims = verify_caller(request, raw_token)
    if isinstance(claims, DispatchRefusal):
        return _refuse(claims, request_id)
    row = await _load_row(request, str(req.message.task_id))
    if row is None or not _visible(row, agent_name, claims):
        return _error(request_id, a2a_error(TASK_NOT_FOUND))
    stored_context = (row.result_json or {}).get("contextId")
    if req.message.context_id and stored_context and req.message.context_id != stored_context:
        return _params_error(request_id, "contextId does not match the task's contextId")
    if row.state in _TERMINAL_STATES:
        return _error(
            request_id,
            a2a_error(
                UNSUPPORTED_OPERATION,
                message=f"the task is {row.state}; send a new message in its contextId instead",
            ),
        )
    return _error(
        request_id,
        a2a_error(
            UNSUPPORTED_OPERATION,
            message=f"the task is {row.state}; a new turn on an unfinished task is not supported until #145",
        ),
    )


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
    row = await _load_row(request, req.id)
    if row is None or not _visible(row, agent_name, claims):
        # One answer for missing, another caller's, and another persona's task.
        return _error(request_id, a2a_error(TASK_NOT_FOUND))
    return _result(request_id, _row_to_v1(row, req.history_length))


def _utc_naive(ts: datetime) -> datetime:
    """A timestamp as the conversations table stores it: UTC wall time. An
    aware value in another zone is converted, not just stripped."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return ts.replace(tzinfo=None)


# The conversations table stores `updated_at` as text in two forms: the server
# default ("2026-10-05 13:35:52", whole seconds) on a row still in flight, and
# SQLAlchemy's ("2026-10-05 13:35:52.123456") once a dispatch records its end.
# Compared as text, the two forms of one instant are unequal. Ordering, the
# cursor and the timestamp filter therefore all compare julianday(updated_at),
# which reads both forms as the same number.
_STATUS_TIME = func.julianday(Conversation.updated_at)


def _encode_page_token(julian: float, task_id: str) -> str:
    raw = json.dumps([julian, task_id])
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii")


def _decode_page_token(token: str) -> tuple[float, str] | None:
    """The (julianday(updated_at), task_id) cursor, or None for a token that
    does not decode to one. A token is not signed, so a caller can write its
    own cursor; it only moves where the page starts, inside the caller's own
    scope."""
    try:
        julian, task_id = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (binascii.Error, UnicodeError, ValueError, TypeError):
        return None
    if isinstance(julian, bool) or not isinstance(julian, (int, float)) or not isinstance(task_id, str):
        return None
    return float(julian), task_id


async def _list_tasks(
    request: Request, agent_name: str, raw_token: str, request_id: Any, params: Any
) -> JSONResponse:
    if not isinstance(params, dict):
        return _params_error(request_id, "ListTasks params must be an object")
    try:
        req = ListTasksRequestV1.model_validate(params)
    except ValidationError as exc:
        return _params_error(request_id, f"invalid ListTasks params: {exc.error_count()} error(s)")
    if _tenant_mismatch(req.tenant, agent_name):
        return _params_error(request_id, f"tenant {req.tenant!r} does not match the path")
    cursor = None
    if req.page_token:
        cursor = _decode_page_token(req.page_token)
        if cursor is None:
            return _params_error(request_id, "invalid pageToken")

    claims = verify_caller(request, raw_token)
    if isinstance(claims, DispatchRefusal):
        return _refuse(claims, request_id)
    is_operator, sub = _owner_filter(claims)
    page_size = req.page_size or _DEFAULT_PAGE_SIZE

    where = [Conversation.target_agent == agent_name]
    if not is_operator:
        if sub is None:
            return _result(
                request_id,
                ListTasksResponseV1(tasks=[], next_page_token="", page_size=page_size, total_size=0).to_wire(),
            )
        where.append(Conversation.caller_agent == sub)
    if req.status is not None and req.status != "TASK_STATE_UNSPECIFIED":
        where.append(Conversation.state == STATE_FROM_V1[req.status])
    if req.status_timestamp_after is not None:
        after = _utc_naive(req.status_timestamp_after).isoformat(sep=" ")
        where.append(_STATUS_TIME >= func.julianday(after))
    if req.context_id:
        # The contextId is in the stored Task; a task still in flight has no
        # stored Task yet, so it cannot match (and carries no contextId on the
        # wire either).
        where.append(Conversation.result_json["contextId"].as_string() == req.context_id)

    page_where = list(where)
    if cursor is not None:
        julian, task_id = cursor
        page_where.append(
            or_(
                _STATUS_TIME < julian,
                and_(_STATUS_TIME == julian, Conversation.task_id < task_id),
            )
        )
    async with open_session(request.app.state.sessionmaker) as session:
        total = (
            await session.execute(select(func.count()).select_from(Conversation).where(*where))
        ).scalar_one()
        found = (
            await session.execute(
                select(Conversation, _STATUS_TIME)
                .where(*page_where)
                .order_by(_STATUS_TIME.desc(), Conversation.task_id.desc())
                .limit(page_size + 1)
            )
        ).all()
    more = len(found) > page_size
    found = found[:page_size]

    tasks = []
    for row, _julian in found:
        task = conversation_to_v1(
            task_id=row.task_id,
            state=row.state,
            result_json=row.result_json,
            updated_at=row.updated_at,
            history_length=req.history_length,
        )
        if req.include_artifacts:
            # Present, even when empty, once the caller asks (spec 3.1.4).
            task.artifacts = task.artifacts or []
        else:
            task.artifacts = None
        tasks.append(task)
    response = ListTasksResponseV1(
        tasks=tasks,
        next_page_token=_encode_page_token(found[-1][1], found[-1][0].task_id) if more else "",
        page_size=page_size,
        total_size=total,
    )
    return _result(request_id, response.to_wire())


async def _runs_here(request: Request, row: Conversation) -> bool:
    """Whether the task's dispatch runs in this process, where the
    AbortCoordinator can reach it: a local handler, not a federated peer."""
    router = request.app.state.dispatch_router
    if not router.has_local(row.target_agent):
        return False
    async with open_session(request.app.state.sessionmaker) as session:
        target = await bulletin_store.get_active(session, row.target_agent)
    return target is None or target.kind != "federated"


async def _cancel_task(
    request: Request, agent_name: str, raw_token: str, request_id: Any, params: Any
) -> JSONResponse:
    if not isinstance(params, dict):
        return _params_error(request_id, "CancelTask params must be an object")
    try:
        req = CancelTaskRequestV1.model_validate(params)
    except ValidationError as exc:
        return _params_error(request_id, f"invalid CancelTask params: {exc.error_count()} error(s)")
    if _tenant_mismatch(req.tenant, agent_name):
        return _params_error(request_id, f"tenant {req.tenant!r} does not match the path")

    claims = verify_caller(request, raw_token)
    if isinstance(claims, DispatchRefusal):
        return _refuse(claims, request_id)
    row = await _load_row(request, req.id)
    if row is None or not _visible(row, agent_name, claims):
        return _error(request_id, a2a_error(TASK_NOT_FOUND))
    if row.state in _TERMINAL_STATES:
        return _error(
            request_id, a2a_error(TASK_NOT_CANCELABLE, message=f"the task is already {row.state}")
        )
    if not await _runs_here(request, row):
        # TaskNotCancelableError stretches "not in a cancelable state" to
        # "not cancelable from here"; #261's baseline is to record it as a
        # deliberate deviation.
        return _error(
            request_id,
            a2a_error(TASK_NOT_CANCELABLE, message="the task runs outside this process; cancel it there"),
        )
    coordinator = request.app.state.dispatch_router.abort_coordinator
    signalled = coordinator.abort(row.task_id, reason="canceled by the caller (CancelTask)")
    if signalled == 0 and not coordinator.is_aborted(row.task_id):
        # Not in flight here: it ended between the read and the abort, or its
        # dispatch did not survive a restart. The first window is wider than
        # it looks: the router drops the task from the AbortGraph before it
        # records the terminal state, so a cancel in that gap gets this answer
        # for a task that is about to read completed.
        return _error(
            request_id, a2a_error(TASK_NOT_CANCELABLE, message="the task is not running")
        )

    deadline = asyncio.get_running_loop().time() + CANCEL_WAIT_SECONDS
    while True:
        row = await _load_row(request, req.id)
        if row is None:  # pragma: no cover - rows are not deleted
            return _error(request_id, a2a_error(TASK_NOT_FOUND))
        if row.state in _TERMINAL_STATES or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(_CANCEL_POLL_SECONDS)
    return _result(request_id, _row_to_v1(row))


def _now() -> datetime:
    """The status timestamp for the task a SendMessage returns."""
    return datetime.now(UTC)
