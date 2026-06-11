"""A2A `.well-known/` endpoints.

`/.well-known/agent-card` returns the signed AgentCard for Cabal Core itself.
`/.well-known/jwks.json` returns the public-key set used to verify caller JWTs
and signed AgentCards.

Phase 1: stub responses so smoke tests can hit the routes. Real AgentCard
signing lands with the bulletin board in `cabal.core.a2a.card`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/.well-known/agent-card", tags=["a2a"])
async def agent_card() -> dict[str, object]:
    return {
        "schema_version": "0.0",
        "name": "cabal-core",
        "skills": [],
        "stub": True,
    }


@router.get("/.well-known/jwks.json", tags=["a2a"])
async def jwks() -> dict[str, object]:
    return {"keys": [], "stub": True}
