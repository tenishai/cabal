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
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update

from cabal.config.identity import CabalConfig, resolve_public_url
from cabal.core.a2a.inbound import inbound_metadata
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
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
    DrainingError,
    GuardrailBlocked,
)
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Conversation, Event
from cabal.core.routes._dispatch_auth import DispatchRefusal, dispatch_token, verify_caller
from cabal.core.scheduler import cancel_scheduled, group_members, parse_not_before

log = structlog.get_logger(__name__)

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
# States in which a task waits on its caller with nothing running.
_PAUSED_STATES = frozenset({"input-required", "auth-required"})
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
        if refusal.remedy:
            # `forbidden` can only carry the message; the remedy rides next
            # to it in the same `{"detail": ...}` shape (#329).
            return JSONResponse(refusal.body_fields(), status_code=403)
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
    return card_response(await signed_agent_card(request, agent_name))


# A card changes when its persona is re-registered, the signing key rotates,
# the public URL changes, cabal.front_door names another persona (the root
# card), or cabal's card builder changes; rarely, so clients may cache it
# briefly and revalidate by ETag (spec 8.6; CARD-CACHE-001, -002).
CARD_MAX_AGE_SECONDS = 300


def card_response(card: dict[str, Any]) -> JSONResponse:
    """A signed card as an HTTP response, with caching headers. The ETag is a
    hash of the card's content, signatures included, so a re-signed card gets
    a new one."""
    digest = hashlib.sha256(json.dumps(card, sort_keys=True).encode("utf-8")).hexdigest()
    return JSONResponse(
        card,
        media_type="application/json",
        headers={
            "Cache-Control": f"public, max-age={CARD_MAX_AGE_SECONDS}",
            "ETag": f'"{digest[:32]}"',
        },
    )


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

    # cabal issue 406: a start time. A past one starts now, as if unset.
    not_before = None
    raw_not_before = (req.metadata or {}).get("cabal_not_before")
    if raw_not_before is not None:
        try:
            not_before = parse_not_before(raw_not_before)
        except ValueError as exc:
            return _params_error(request_id, str(exc))
        if not_before <= datetime.now(UTC):
            not_before = None
        elif not (cfg is not None and cfg.return_immediately):
            return _error(
                request_id,
                a2a_error(
                    UNSUPPORTED_OPERATION,
                    message="cabal_not_before needs returnImmediately: a scheduled task can't hold the request open",
                ),
            )
    # cabal issue 423: a task for a group, sent through one of its members. It
    # waits like a scheduled task (from now, or from cabal_not_before) and the
    # Scheduler starts it on the first idle member.
    group = (req.metadata or {}).get("cabal_group")
    if group is not None:
        members = group_members(request.app.state, group) if isinstance(group, str) else None
        if not members:
            return _params_error(request_id, f"cabal_group {group!r} is not a group in [groups]")
        if agent_name not in members:
            return _params_error(request_id, f"{agent_name} is not a member of group {group!r}")
        if not (cfg is not None and cfg.return_immediately):
            return _error(
                request_id,
                a2a_error(
                    UNSUPPORTED_OPERATION,
                    message="cabal_group needs returnImmediately: a group task waits for an idle member",
                ),
            )
    asked_for = not_before  # the caller's own start time, if any
    if group is not None and not_before is None:
        not_before = datetime.now(UTC)
    # The caller's own claims: what a scheduled task (issue 406) and a 705
    # "waiting" reply's continuation run under. Never the token itself.
    verified = verify_caller(request, raw_token)
    if isinstance(verified, DispatchRefusal):  # pragma: no cover - dispatch_token verified it
        return _refuse(verified, request_id)
    schedule_claims: dict[str, Any] = {
        k: verified[k] for k in ("sub", "cabal_persona", "cabal_scope", "aud", "iat", "exp") if k in verified
    }
    if not_before is not None:
        exp = verified.get("exp")
        if isinstance(exp, (int, float)) and not_before.timestamp() >= exp:
            return _params_error(
                request_id,
                "cabal_not_before is after your token expires; the caller's authority must cover the start",
            )

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
            "metadata": inbound_metadata(req.metadata),
        }
    )
    if asked_for is None:
        task.metadata.pop("cabal_not_before", None)
    else:
        task.metadata["cabal_not_before"] = asked_for.isoformat()
    if group is not None:
        task.metadata["cabal_group"] = group
        # Kept for every later round: an answer re-queues the task to whichever
        # member is idle, which may not have seen the brief (issue 423).
        task.metadata["cabal_group_brief"] = _text_of(message)
    else:
        # The same for any task (issue 145): its history may open with an
        # earlier conversation replayed from its contextId, so the brief is
        # recorded here, not looked for there.
        task.metadata["cabal_brief"] = _text_of(message)
    detach = cfg is not None and bool(cfg.return_immediately)
    detached = request.app.state.detached
    reserve = detach and not_before is None
    if reserve and not detached.try_reserve():
        return _error(
            request_id,
            a2a_error(
                UNSUPPORTED_OPERATION,
                message=(
                    "too many tasks are running detached; retry later, "
                    "or send without returnImmediately"
                ),
            ),
        )
    try:
        try:
            if not_before is not None:
                response = await request.app.state.dispatch_router.schedule(
                    target_agent=agent_name, task=task, caller_jwt=token,
                    not_before=not_before, claims=schedule_claims, target_group=group,
                )
            elif detach:
                # The start phase runs here, so auth is checked and the task's
                # row exists before the reply; the run phase goes to the
                # registry and the client polls GetTask (#277).
                started, run = await request.app.state.dispatch_router.start_detached(
                    target_agent=agent_name, target_generation=None, task=task, caller_jwt=token,
                    caller_claims=schedule_claims,
                )
                detached.spawn(started.id, run)
                response = started
            else:
                response = await request.app.state.dispatch_router.dispatch(
                    target_agent=agent_name, target_generation=None, task=task, caller_jwt=token,
                    caller_claims=schedule_claims,
                )
        except BaseException:
            if reserve:
                detached.release()  # the reserved slot did not become a run
            raise
    except (AgentNotFoundError, ConfigurationError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CallerJWTError as exc:
        raise unauthenticated(str(exc)) from exc
    except GuardrailBlocked as exc:  # before DispatchError, its base class
        raise forbidden(str(exc)) from exc
    except DrainingError as exc:  # before DispatchError too (issue 405)
        return _error(request_id, a2a_error(UNSUPPORTED_OPERATION, message=str(exc)))
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


def _visible(
    row: Conversation | None, agent_name: str, claims: dict[str, Any], app_state: Any = None
) -> bool:
    """Whether the caller may see `row`: a task on this persona that the
    caller sent, or any task on it for the operator. A group task (cabal
    issue 423) is on every member's persona, since which member runs it is
    settled only when it starts."""
    if row is None:
        return False
    if row.target_agent != agent_name and not (
        row.target_group is not None
        and agent_name in (group_members(app_state, row.target_group) or [])
    ):
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
    """A SendMessage that names a task (spec 3.4.3): the answer to a task that
    stopped in `input-required` (issue 145). Any other unfinished state is
    refused: a new turn on a task that is still running or queued has nothing
    to answer."""
    claims = verify_caller(request, raw_token)
    if isinstance(claims, DispatchRefusal):
        return _refuse(claims, request_id)
    row = await _load_row(request, str(req.message.task_id))
    if row is None or not _visible(row, agent_name, claims, request.app.state):
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
    if row.state == "input-required":
        return await _answer_task(request, row, claims, req, request_id)
    return _error(
        request_id,
        a2a_error(
            UNSUPPORTED_OPERATION,
            message=f"the task is {row.state}; only a task in input-required takes an answer",
        ),
    )


def _text_of(message: Message | None) -> str:
    if message is None:
        return ""
    return "\n".join(p.text for p in message.parts if isinstance(p, TextPart)).strip()


def _brief_of(prior: Task) -> str:
    """The task's own brief, recorded at send time (`cabal_group_brief` for a
    group task, issue 423; `cabal_brief` for any other, issue 145).

    A task sent before that, or through another door, has none recorded. Its
    stored history is the conversation replayed from its contextId followed by
    its own message (`router.py`, the stored-history rule), so the brief is the
    LAST user message there. The first would be an earlier task's request."""
    kept = prior.metadata.get("cabal_group_brief") or prior.metadata.get("cabal_brief")
    if kept:
        return str(kept)
    for message in reversed(prior.history):
        if message.role == "user":
            return _text_of(message)
    return ""


async def _answer_task(
    request: Request, row: Conversation, claims: dict[str, Any], req: SendMessageRequestV1, request_id: Any
) -> JSONResponse:
    """An answer to a task that ended asking a question (issues 423 and 145).

    The agent that asked is already free: its turn ended `input-required`, so
    nothing was held while the question waited. The answer puts the task back
    in the queue, under the same id and contextId: a group task for whichever
    member is idle next (the one that asked, if it is), any other task for the
    agent that asked. A group member may not have seen the task, and an agent
    keeps no state between turns, so the message carries the brief, the
    asker's hand-off and the answer. Work in progress lives in git and the
    task's doc, not here: cabal carries only the text."""
    try:
        answer = _text_of(message_from_v1(req.message))
    except UnsupportedPartError as exc:
        return _error(request_id, a2a_error(CONTENT_TYPE_NOT_SUPPORTED, message=str(exc)))
    if not answer:
        return _params_error(request_id, "an answer needs some text")
    if not row.result_json:
        # Every dispatch stores its task; a row without one has nothing to continue.
        return _error(
            request_id,
            a2a_error(UNSUPPORTED_OPERATION, message="the task has no stored state to continue"),
        )
    prior = Task.model_validate(row.result_json)
    asker = row.target_agent
    answerer = claims.get("sub") or "the operator"
    group = row.target_group
    brief = _brief_of(prior)
    round_key = "cabal_group_round" if group is not None else "cabal_answer_round"
    rounds = int(prior.metadata.get(round_key) or 1) + 1
    sent_to = f", sent to group {group}" if group is not None else ""
    text = (
        f"This continues task {row.task_id}{sent_to}. {asker} stopped with "
        f"questions; {answerer} answered. Pick up from the hand-off: the work so far is in git and the "
        f"task's doc.\n\n## Brief\n\n{brief or '(not recorded)'}\n\n"
        f"## Hand-off from {asker}\n\n{_text_of(prior.status.message) or '(none)'}\n\n"
        f"## Answer from {answerer}\n\n{answer}"
    )
    history = [*prior.history, *([prior.status.message] if prior.status.message is not None else [])]
    task = prior.model_copy(
        update={
            "status": TaskStatus(state="submitted", message=Message(role="user", parts=[TextPart(text=text)])),
            "history": history,
            "metadata": {**prior.metadata, "cabal_asked_by": asker, round_key: rounds},
        }
    )
    stored = {k: claims[k] for k in ("sub", "cabal_persona", "cabal_scope", "aud", "iat", "exp") if k in claims}
    async with open_session(request.app.state.sessionmaker) as session:
        moved = await session.execute(
            update(Conversation)
            .where(Conversation.task_id == row.task_id, Conversation.state == "input-required")
            .values(
                state="scheduled",
                not_before=datetime.now(UTC),
                result_json=task.model_dump(by_alias=True, exclude_none=True),
                schedule_json=stored,
                updated_at=datetime.now(UTC),
            )
        )
        if moved.rowcount != 1:
            return _error(
                request_id,
                a2a_error(UNSUPPORTED_OPERATION, message="the task changed state while being answered; read it again"),
            )
        session.add(
            Event(
                task_id=row.task_id,
                event_type="dispatch_group_answered" if group is not None else "dispatch_answered",
                payload_json={"group": group, "asked_by": asker, "answered_by": answerer, "round": rounds},
            )
        )
    log.info(
        "dispatch.group_answered" if group is not None else "dispatch.answered",
        task_id=row.task_id, group=group, asked_by=asker, round=rounds,
    )
    refreshed = await _load_row(request, row.task_id)
    assert refreshed is not None
    return _result(request_id, {"task": _row_to_v1(refreshed)})


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
    if row is None or not _visible(row, agent_name, claims, request.app.state):
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

    # The tasks on this persona, as `_visible` reads them: its own, and a group
    # task of any group it is in, whichever member runs it (cabal issue 314: an
    # outsider holding one member's token must find a question another member
    # stopped with).
    identity = getattr(request.app.state, "identity", None)
    groups = [g for g, members in (identity.groups if identity is not None else {}).items() if agent_name in members]
    on_persona = Conversation.target_agent == agent_name
    if groups:
        on_persona = or_(on_persona, Conversation.target_group.in_(groups))
    where = [on_persona]
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


async def _cancel_paused(request: Request, row: Conversation) -> dict[str, Any]:
    """Mark a paused task canceled and return it, keeping its stored
    conversation (history, contextId)."""
    stored = Task.model_validate(row.result_json) if row.result_json else Task(
        id=row.task_id, status=TaskStatus(state="submitted")
    )
    canceled = stored.model_copy(
        update={
            "status": TaskStatus(
                state="canceled",
                message=Message(role="agent", parts=[TextPart(text="canceled by the caller")]),
            )
        }
    )
    async with open_session(request.app.state.sessionmaker) as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.task_id == row.task_id)
            .values(
                state="canceled",
                updated_at=datetime.now(UTC),
                result_json=canceled.model_dump(by_alias=True, exclude_none=True),
            )
        )
    refreshed = await _load_row(request, row.task_id)
    assert refreshed is not None
    return _row_to_v1(refreshed)


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
    if row is None or not _visible(row, agent_name, claims, request.app.state):
        return _error(request_id, a2a_error(TASK_NOT_FOUND))
    if row.state in _TERMINAL_STATES:
        return _error(
            request_id, a2a_error(TASK_NOT_CANCELABLE, message=f"the task is already {row.state}")
        )
    if row.state in _PAUSED_STATES:
        # Paused, waiting on the caller: nothing is running behind it, so it
        # is canceled here (CORE-CANCEL-001).
        return _result(request_id, await _cancel_paused(request, row))
    if row.state == "scheduled":
        # Not started yet (cabal issue 406). If the Scheduler claimed it in
        # the meantime, it is running now: cancel it as a running task.
        if await cancel_scheduled(request.app.state.sessionmaker, row.task_id):
            refreshed = await _load_row(request, row.task_id)
            assert refreshed is not None
            return _result(request_id, _row_to_v1(refreshed))
        row = await _load_row(request, row.task_id)
        assert row is not None
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
