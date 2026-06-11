"""Authentication routes.

POST /auth/bootstrap — consume a bootstrap code, mint a JWT.

The code is one-shot: it succeeds exactly once or returns 401 with a
specific error code so the client can distinguish unknown/expired/used.
"""

from __future__ import annotations

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
        sub = "operator:dimartin"
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
