"""A2A Task endpoint.

POST /a2a/tasks — accept a Task, hand it to the dispatch router, return
the response Task.

Caller-token semantics:
    - If the inbound bearer carries "operator" or "colleague" scope
      (aud="cabal-core"), the endpoint mints a fresh per-target JWT
      internally and uses it for the dispatch. Neither has to pre-mint
      per-target tokens.
    - If the inbound bearer is already aimed at the target (aud matches),
      we forward it as-is to the dispatch router.
    - If the inbound bearer carries "colleague" scope (not yet aimed at
      the target), the endpoint consults the room-synced `dispatch_pairs`
      table: an allowed (caller, target) pair re-mints the same way an
      operator token does; a missing pair is a 403 naming both sides. An
      empty table means every such request is allowed -- enforcement only
      begins once the room syncs its first set. The gate is SCOPE, not
      the shape of the sub: any caller string is looked up in the table.

Phase 1 surface — the A2A JSON-RPC envelope lands in Phase 2 with
streaming + tasks/get + resubscribe.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from cabal.core.a2a.types import Task
from cabal.core.a2a.version import requested_version
from cabal.core.auth.jwt import extract_bearer
from cabal.core.broker.router import (
    AgentNotFoundError,
    CallerJWTError,
    DispatchError,
    GuardrailBlocked,
)
from cabal.core.routes._dispatch_auth import DispatchRefusal, dispatch_token

router = APIRouter(prefix="/a2a", tags=["a2a"])


class TaskRequest(BaseModel):
    # `target` is the explicit-dispatch path. `required_capabilities` is
    # the late-binding path — when target is null, the router picks any
    # registered agent whose capability set is a superset of the listed
    # requirements. Exactly one of (target, required_capabilities) must
    # be supplied.
    target: str | None = None
    target_generation: int | None = None
    required_capabilities: list[str] | None = None
    task: Task



async def _resolve_dispatch_token(
    request: Request, raw_token: str, target: str
) -> str | JSONResponse:
    """The token to hand the dispatch router, by the shared policy in
    `_dispatch_auth.dispatch_token`. Raises HTTPException on a refusal: 401
    for an invalid or revoked token or one with no sub, 403 for a token the
    policy refuses, 500 for a server-side fault. A refusal that names a
    remedy is returned as a response instead, with `remedy` beside `detail`
    in the same body shape, because HTTPException can carry only `detail`
    (#329)."""
    token = await dispatch_token(request, raw_token, target)
    if isinstance(token, DispatchRefusal):
        status = {"unauthenticated": 401, "forbidden": 403, "internal": 500}[token.kind]
        if token.remedy:
            return JSONResponse(token.body_fields(), status_code=status)
        raise HTTPException(status_code=status, detail=token.message)
    return token


@router.post("/tasks", response_model=Task)
async def submit_task(
    req: TaskRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Task | JSONResponse:
    # cabal's own routing envelope, not an A2A interface: a client that asks
    # for an A2A version is refused rather than silently served (#265).
    if requested_version(request) is not None:
        raise HTTPException(
            status_code=400,
            detail="this route serves no A2A version; send no A2A-Version header",
        )
    raw_token = extract_bearer(authorization)
    router_ = request.app.state.dispatch_router

    # Resolve target: explicit-dispatch takes the target verbatim;
    # late-binding looks up an agent that satisfies the capability
    # requirement.
    target = req.target
    if target is None:
        if not req.required_capabilities:
            raise HTTPException(
                status_code=400,
                detail=(
                    "request must specify either `target` (explicit "
                    "dispatch) or `required_capabilities` (late-binding)"
                ),
            )
        target = router_.select_by_capabilities(req.required_capabilities)
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no registered agent satisfies required_capabilities="
                    f"{req.required_capabilities}"
                ),
            )

    dispatch_token = await _resolve_dispatch_token(request, raw_token, target)
    if isinstance(dispatch_token, JSONResponse):
        return dispatch_token
    try:
        return await router_.dispatch(
            target_agent=target,
            target_generation=req.target_generation,
            task=req.task,
            caller_jwt=dispatch_token,
        )
    except CallerJWTError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # BEFORE DispatchError, WHICH IS ITS BASE (cabal#152). 502 says the upstream
    # failed and the request was fine; a terminal guardrail verdict says the
    # opposite, and 502 is what a client retries. See the matching arm in
    # a2a_jsonrpc._message_send.
    except GuardrailBlocked as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DispatchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
