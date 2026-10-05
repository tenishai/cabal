"""A2A `.well-known/` endpoints.

`/.well-known/agent-card` returns a stub card for Cabal Core itself (unsigned; see #261).
`/.well-known/agent-card.json` returns the A2A v1.0 card of the front-door persona
(`cabal.front_door`, #270), or 404 when none is configured.
`/.well-known/jwks.json` returns the public-key set used to verify caller JWTs
and signed AgentCards.

Phase 1: agent-card is still a stub (real signing lands with the bulletin
board). JWKS reads from the `signing_keys` table.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from cabal.config.identity import resolve_front_door
from cabal.core.auth.keys import list_public_jwks
from cabal.core.db import open_session
from cabal.core.routes.a2a_v1 import card_response, signed_agent_card

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


@router.get("/.well-known/agent-card.json", tags=["a2a"])
async def front_door_card(request: Request) -> JSONResponse:
    """The A2A v1.0 discovery card for this host (spec 8.2): the front-door
    persona's own card, whose interface is that persona's endpoint. cabal has
    no endpoint that answers as cabal-core, and a well-known card must name a
    callable interface, so the root card is a persona's (#270)."""
    agent_name = resolve_front_door(getattr(request.app.state, "identity", None))
    if agent_name is None:
        raise HTTPException(
            status_code=404, detail="no front door is configured (cabal.front_door)"
        )
    return card_response(await signed_agent_card(request, agent_name, front_door=True))


@router.get("/.well-known/jwks.json", tags=["a2a"])
async def jwks(request: Request) -> dict[str, object]:
    sessionmaker = request.app.state.sessionmaker
    async with open_session(sessionmaker) as session:
        keys = await list_public_jwks(session)
    return {"keys": keys}
