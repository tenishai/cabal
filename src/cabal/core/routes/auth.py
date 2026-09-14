"""Authentication routes.

POST /auth/bootstrap — consume a bootstrap code, mint a JWT.

The code is one-shot: it succeeds exactly once or returns 401 with a
specific error code so the client can distinguish unknown/expired/used.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from cabal.core.auth.bootstrap_codes import BootstrapCodeError, consume
from cabal.core.auth.jwt import OPERATOR_SUB_PREFIX, JWTError, extract_bearer, mint, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session

router = APIRouter(prefix="/auth", tags=["auth"])


class BootstrapRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


class BootstrapResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    persona: str | None = None


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap(req: BootstrapRequest, request: Request) -> BootstrapResponse:
    sessionmaker = request.app.state.sessionmaker
    master_key = request.app.state.master_key

    # Run the entire bootstrap inside the transaction so that ANY failure —
    # missing signing key, unconfigured operator_id, mint error — rolls back
    # the consume() below (open_session rolls back on exception). A one-shot
    # code exists for replay prevention, not misconfiguration punishment: a
    # server-side 500 must not burn it. Previously the operator_id check and
    # mint ran *after* the block committed, so a config mistake spent the
    # operator's code and cost a daemon restart. See cabal#78.
    async with open_session(sessionmaker) as session:
        try:
            persona_name = await consume(session, req.code)
        except BootstrapCodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc

        stored = await load_active_signing_key(session, master_key=master_key)
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="no active signing key",
            )

        if persona_name is None:
            identity = getattr(request.app.state, "identity", None)
            operator_id = (
                (identity.cabal.operator_id if identity is not None else None)
                or os.environ.get("CABAL_OPERATOR_ID")
            )
            if not operator_id:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        "operator_id not configured; "
                        "set cabal.operator_id in identity.toml or CABAL_OPERATOR_ID env var"
                    ),
                )
            sub = f"{OPERATOR_SUB_PREFIX}{operator_id}"
            aud = "cabal-core"
            scope = ["operator"]
        else:
            sub = f"persona:{persona_name}"
            aud = persona_name
            scope = ["dispatch"]

        token = mint(
            signing_key=stored,
            sub=sub,
            aud=aud,
            persona=persona_name,
            scope=scope,
            ttl_seconds=60 * 60,  # 1h — operator token; Tasks use session_token for >5min
        )

    return BootstrapResponse(access_token=token, persona=persona_name)


# ---- gateway token exchange (external-MCP arc, C1) ------------------------

_LIMINAL_TTL_SECONDS = 15 * 60


class LiminalTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    aud: str
    persona: str
    expires_at: str


@router.post("/liminal-token", response_model=LiminalTokenResponse)
async def liminal_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> LiminalTokenResponse:
    """Exchange a colleague token for a short-lived gateway token.

    The colleague JWT is standing identity (year-scale, aud cabal-core);
    the MCP gateway deliberately demands its own audience so standing
    identity cannot open it directly. This route is the swap: prove you
    are an agent this switchboard minted, get a fifteen-minute
    aud="liminal" token whose persona is your agent name -- the key the
    gateway's policy store grants on. Nothing here grants anything: an
    exchanged token for an ungranted agent opens exactly zero tools, and
    a leaked one dies inside the quarter hour.

    Colleague-scope only, deliberately: operators already hold
    /admin/token/mint-service for service tokens, and a dispatch-scoped
    persona token is a call credential, not a standing identity to
    exchange.

    The gate is inlined rather than borrowed from the admin module. That
    was written while the shared scope helper was still in flight; it has
    since LANDED (`admin._require_scope`, cabal#124), so folding this route
    into it is now a live follow-up rather than a future one.

    Two things to carry across if you do. There are THREE gates here, not
    the two this sentence used to imply: a missing bearer dies in
    `extract_bearer` before `verify()` runs, then audience, then scope.
    And audience is enforced INSIDE `verify(expected_audience="cabal-core")`,
    so a validly-signed token aimed elsewhere gets 401 here — while
    `_require_scope` verifies with `expected_audience=None` and checks `aud`
    by hand, giving 403 per CABAL-DECISIONS D-017. Folding without deciding
    which status is right would change this route's contract silently.
    """
    try:
        claims = verify(
            extract_bearer(authorization),
            key_set=request.app.state.jwks_provider(),
            expected_audience="cabal-core",
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    if "colleague" not in (claims.get("cabal_scope") or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requires colleague scope",
        )
    sub = str(claims.get("sub") or "")
    if sub.startswith("agent:"):
        persona = sub.split(":", 1)[1]
    else:
        persona = str(claims.get("cabal_persona") or "")
    if not persona:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token carries no agent identity to exchange",
        )

    async with open_session(request.app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=request.app.state.master_key)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="no active signing key",
        )

    token = mint(
        signing_key=stored,
        sub=sub,
        aud="liminal",  # must match admin.py's _AUD_LIMINAL / the gateway registry
        persona=persona,
        scope=["mcp"],
        ttl_seconds=_LIMINAL_TTL_SECONDS,
    )
    expires_at = (datetime.now(UTC) + timedelta(seconds=_LIMINAL_TTL_SECONDS)).isoformat()
    return LiminalTokenResponse(
        access_token=token,
        aud="liminal",
        persona=persona,
        expires_at=expires_at,
    )
