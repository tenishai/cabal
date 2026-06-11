"""Operator-only admin routes.

GET /admin/agents — list every row in the bulletin board (active, draining,
retired).

Auth: requires a token with `cabal_scope` containing 'operator'. The
returned listing intentionally includes safety_tier + last_heartbeat so
the operator can spot stuck handlers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from cabal.core.auth.jwt import JWTError, verify
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session

router = APIRouter(prefix="/admin", tags=["admin"])


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail="missing or malformed Authorization header")
    return authorization.split(" ", 1)[1].strip()


def _require_operator_claims(request: Request, raw_token: str) -> dict[str, Any]:
    try:
        claims = verify(
            raw_token,
            key_set=request.app.state.jwks_provider(),
            expected_audience=None,
        )
    except JWTError as exc:
        raise HTTPException(401, detail=str(exc)) from exc
    if "operator" not in (claims.get("cabal_scope") or []):
        raise HTTPException(403, detail="operator scope required")
    return claims


@router.get("/agents")
async def list_agents(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator_claims(request, _extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        rows = await bulletin_store.list_all(session)
    return {
        "agents": [
            {
                "name": r.name,
                "generation": r.generation,
                "persona": r.persona_name,
                "role": r.role,
                "status": r.status,
                "safety_tier": r.safety_tier,
                "endpoint": r.endpoint,
                "kid": r.agent_card_kid,
                "last_heartbeat": r.last_heartbeat.isoformat() if r.last_heartbeat else None,
                "drain_started_at": r.drain_started_at.isoformat() if r.drain_started_at else None,
            }
            for r in rows
        ]
    }
