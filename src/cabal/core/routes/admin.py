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

from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import select

from cabal.core.a2a.card import sign_card
from cabal.core.a2a.types import AgentCard, AgentCapabilities, Skill
from cabal.core.auth.jwt import JWTError, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Agent

router = APIRouter(prefix="/admin", tags=["admin"])


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail="missing or malformed Authorization header")
    return authorization.split(" ", 1)[1].strip()


def _require_operator_claims(
    request: Request, raw_token: str
) -> dict[str, Any]:
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


class PrimusRotateRequest(BaseModel):
    persona: str
    hard: bool = False  # Phase L: hard kill is a stub; v1 always drains


@router.post("/primus/rotate")
async def primus_rotate(
    req: PrimusRotateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Rotate a primus generation: mark current active as draining, insert
    fresh active with generation+1 carrying a new signed AgentCard.

    Phase L v1: in-process LocalHandler for the agent name does NOT
    re-instantiate. The bulletin board reflects the rotation; subordinate
    return-routing keeps working (the schema supports two coexisting
    generations); but the Python handler the new-gen calls is the SAME
    object. Phase L v2 will orchestrate handler replacement.
    """
    claims = _require_operator_claims(
        request, _extract_bearer(authorization)
    )
    agent_name = req.persona if req.persona.endswith("-primus") else f"{req.persona}-primus"

    async with open_session(request.app.state.sessionmaker) as session:
        # 1. Find the current active row
        existing = await session.execute(
            select(Agent).where(
                Agent.name == agent_name, Agent.status == "active"
            )
        )
        current = existing.scalar_one_or_none()
        if current is None:
            raise HTTPException(
                404, detail=f"no active row for primus {agent_name!r}"
            )

        # 2. Mint a fresh signed AgentCard for the new generation
        stored = await load_active_signing_key(
            session, master_key=request.app.state.master_key
        )
        if stored is None:
            raise HTTPException(500, detail="no active signing key")
        new_generation = current.generation + 1
        card = AgentCard(
            name=agent_name,
            url=current.endpoint,
            version="0.1.0",
            skills=[Skill(id=agent_name, name=agent_name)],
            capabilities=AgentCapabilities(streaming=False),
            metadata={
                "cabal_persona": current.persona_name,
                "cabal_generation": new_generation,
                "cabal_safety_tier": current.safety_tier,
                "cabal_role": current.role,
            },
        )
        signed = sign_card(card, stored)

        # 3. Mark current draining + insert new active
        current.status = "draining"
        current.drain_started_at = datetime.now(timezone.utc)
        await bulletin_store.register_agent(
            session,
            name=agent_name,
            persona_name=current.persona_name,
            role=current.role,
            endpoint=current.endpoint,
            capabilities=current.capabilities_json,
            safety_tier=current.safety_tier,
            agent_card_json={"jws": signed, "card": card.model_dump()},
            agent_card_kid=stored.kid,
            generation=new_generation,
        )

    return {
        "rotated": True,
        "agent": agent_name,
        "previous_generation": current.generation,
        "new_generation": new_generation,
        "operator": claims.get("sub"),
        "note": "in-process LocalHandler unchanged in Phase L v1",
    }
