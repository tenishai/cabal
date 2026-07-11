"""Authentication routes.

POST /auth/bootstrap — consume a bootstrap code, mint a JWT.

The code is one-shot: it succeeds exactly once or returns 401 with a
specific error code so the client can distinguish unknown/expired/used.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from cabal.core.auth.bootstrap_codes import BootstrapCodeError, consume
from cabal.core.auth.jwt import mint
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
            sub = f"operator:{operator_id}"
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
