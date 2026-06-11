"""A2A Task endpoint.

POST /a2a/tasks — accept a Task, hand it to the dispatch router, return
the response Task.

Caller-token semantics:
    - If the inbound bearer is an OPERATOR token (scope contains
      "operator", aud="cabal-core"), the endpoint mints a fresh per-target
      JWT internally and uses it for the dispatch. Operators don't have to
      pre-mint per-target tokens.
    - If the inbound bearer is already aimed at the target (aud matches),
      we forward it as-is to the dispatch router.

Phase 1 surface — A2A v1.0 JSON-RPC envelope lands in Phase 2 with
streaming + tasks/get + resubscribe.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from cabal.core.a2a.types import Task
from cabal.core.auth.jwt import JWTError, mint, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.broker.router import DispatchError
from cabal.core.db import open_session

router = APIRouter(prefix="/a2a", tags=["a2a"])


class TaskRequest(BaseModel):
    target: str
    target_generation: int | None = None
    task: Task


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    return authorization.split(" ", 1)[1].strip()


async def _resolve_dispatch_token(
    request: Request, raw_token: str, target: str
) -> str:
    """Decide which token to hand the dispatch router.

    Returns the same token if it's already aimed at `target`; else, if the
    inbound token is an operator token, mints a fresh per-target JWT.
    Raises HTTPException on any auth failure.
    """
    jwks_provider = request.app.state.jwks_provider
    try:
        claims = verify(raw_token, key_set=jwks_provider(), expected_audience=None)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if claims.get("aud") == target:
        return raw_token

    scope = claims.get("cabal_scope") or []
    if "operator" not in scope:
        raise HTTPException(
            status_code=403,
            detail=f"token aud={claims.get('aud')!r} cannot dispatch to {target!r}",
        )

    # Operator → re-mint as a per-target token.
    sessionmaker = request.app.state.sessionmaker
    master_key = request.app.state.master_key
    async with open_session(sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=master_key)
    if stored is None:
        raise HTTPException(status_code=500, detail="no active signing key")
    return mint(
        signing_key=stored,
        sub=claims.get("sub", "operator"),
        aud=target,
        persona=claims.get("cabal_persona"),
        scope=["dispatch"],
    )


@router.post("/tasks", response_model=Task)
async def submit_task(
    req: TaskRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Task:
    raw_token = _extract_bearer(authorization)
    dispatch_token = await _resolve_dispatch_token(request, raw_token, req.target)
    router_ = request.app.state.dispatch_router
    try:
        return await router_.dispatch(
            target_agent=req.target,
            target_generation=req.target_generation,
            task=req.task,
            caller_jwt=dispatch_token,
        )
    except DispatchError as exc:
        msg = str(exc)
        if "caller JWT" in msg:
            raise HTTPException(status_code=401, detail=msg) from exc
        if "no agent registered" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=502, detail=msg) from exc
