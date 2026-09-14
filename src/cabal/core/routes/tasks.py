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
    - If the inbound bearer's sub is "agent:<name>" (not yet aimed at the
      target), the endpoint consults the room-synced `dispatch_pairs`
      table (Task C3): an allowed (caller, target) pair re-mints the same
      way an operator token does; a missing pair is a 403 naming both
      sides. An empty table means every such request is allowed --
      enforcement only begins once the room syncs its first set.

Phase 1 surface — A2A v1.0 JSON-RPC envelope lands in Phase 2 with
streaming + tasks/get + resubscribe.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from cabal.core import dispatch_pairs
from cabal.core.a2a.types import Task
from cabal.core.auth.jwt import JWTError, extract_bearer, mint, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.broker.router import (
    AgentNotFoundError,
    CallerJWTError,
    DispatchError,
    GuardrailBlocked,
)
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session

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
) -> str:
    """Decide which token to hand the dispatch router.

    Returns the same token if it's already aimed at `target`; else, if the
    inbound token is an operator token, or an "agent:<name>" token the
    room's dispatch_pairs allow (Task C3), mints a fresh per-target JWT.
    Raises HTTPException on any auth failure.
    """
    jwks_provider = request.app.state.jwks_provider
    try:
        claims = verify(raw_token, key_set=jwks_provider(), expected_audience=None)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    # Determine the audience the dispatch token must carry. For a `federated`
    # target the token is forwarded to the sovereign peer, so its `aud` is the
    # sovereign service id — not the bare agent name (cabal#45).
    #
    # Single session: the bulletin lookup and (only on the operator re-mint
    # branch) the signing-key fetch share one open_session block. The signing
    # key is loaded LAZILY so the fast path (token already aimed at `audience`)
    # and the 403 return exit without Fernet-decrypting the key on every
    # dispatch.
    sessionmaker = request.app.state.sessionmaker
    master_key = request.app.state.master_key
    async with open_session(sessionmaker) as session:
        row = await bulletin_store.get_active(session, target)

        if row is not None and row.kind == "federated":
            if not row.federation_audience:
                raise HTTPException(
                    status_code=500,
                    detail=f"federated target {target!r} has no federation_audience",
                )
            audience = row.federation_audience
        else:
            audience = target

        if claims.get("aud") == audience:
            return raw_token

        scope = claims.get("cabal_scope") or []
        sub = claims.get("sub") or ""
        if "operator" not in scope:
            # Room-owned policy (Task C3): a caller whose sub is
            # "agent:<name>" is asking to be re-minted toward a NEW
            # target. Whether that's allowed is decided by the flat
            # dispatch_pairs table the room syncs via
            # PUT /admin/dispatch-pairs -- an empty table means every
            # such request is open (enforcement starts at the first
            # sync). Operators never reach this branch (checked above).
            # Any other sub (a persona/service token, not an agent) gets
            # the same plain rejection it always has.
            if sub.startswith("agent:"):
                if not await dispatch_pairs.is_allowed(session, caller=sub, target=target):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"{sub} is not a colleague of {target} yet. "
                            "Ask in the room."
                        ),
                    )
            else:
                raise HTTPException(
                    status_code=403,
                    detail=f"token aud={claims.get('aud')!r} cannot dispatch to {target!r}",
                )

        # Operator or an allowed colleague → re-mint as a per-target token
        # aimed at `audience`. Only now do we need the signing key, so
        # load it lazily within this session.
        stored = await load_active_signing_key(session, master_key=master_key)

    if stored is None:
        raise HTTPException(status_code=500, detail="no active signing key")
    return mint(
        signing_key=stored,
        sub=claims.get("sub", "operator"),
        aud=audience,
        persona=claims.get("cabal_persona"),
        scope=["dispatch"],
    )


@router.post("/tasks", response_model=Task)
async def submit_task(
    req: TaskRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Task:
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
