"""A2A `.well-known/` endpoints.

`/.well-known/agent-card` returns a stub card for Cabal Core itself (unsigned; see #261).
`/.well-known/jwks.json` returns the public-key set used to verify caller JWTs
and signed AgentCards.

Phase 1: agent-card is still a stub (real signing lands with the bulletin
board). JWKS reads from the `signing_keys` table.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from cabal.core.auth.keys import list_public_jwks
from cabal.core.db import open_session

router = APIRouter()


@router.get("/.well-known/agent-card", tags=["a2a"])
async def agent_card(request: Request) -> dict[str, object]:
    return {
        "schema_version": "0.0",
        "name": "cabal-core",
        "skills": [],
        "kid": getattr(request.app.state, "active_kid", None),
        "stub": True,
    }


@router.get("/.well-known/jwks.json", tags=["a2a"])
async def jwks(request: Request) -> dict[str, object]:
    sessionmaker = request.app.state.sessionmaker
    async with open_session(sessionmaker) as session:
        keys = await list_public_jwks(session)
    return {"keys": keys}
