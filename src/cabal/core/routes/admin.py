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

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select

from cabal.core.a2a.card import sign_card
from cabal.core.a2a.types import AgentCard, AgentCapabilities, Skill
from cabal.core.auth.jwt import JWTError, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Agent, Conversation, Event

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


# ---- Conversations browser (Phase U) ----------------------------------


@router.get("/conversations")
async def list_conversations(
    request: Request,
    authorization: str | None = Header(default=None),
    limit: int = 50,
    state: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Most-recent first, filterable. Snippets only (no full result_json)."""
    _require_operator_claims(request, _extract_bearer(authorization))
    limit = max(1, min(limit, 200))
    async with open_session(request.app.state.sessionmaker) as session:
        stmt = select(Conversation).order_by(desc(Conversation.updated_at))
        if state is not None:
            stmt = stmt.where(Conversation.state == state)
        if target is not None:
            stmt = stmt.where(Conversation.target_agent == target)
        stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

    return {
        "conversations": [
            {
                "task_id": r.task_id,
                "target_agent": r.target_agent,
                "target_generation": r.target_generation,
                "caller_agent": r.caller_agent,
                "state": r.state,
                "cabal_escalated_from": r.cabal_escalated_from,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "result_snippet": _snippet(r.result_json) if r.result_json else None,
            }
            for r in rows
        ]
    }


@router.get("/conversations/{task_id}")
async def get_conversation(
    request: Request,
    task_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator_claims(request, _extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        conv = (
            await session.execute(
                select(Conversation).where(Conversation.task_id == task_id)
            )
        ).scalar_one_or_none()
        if conv is None:
            raise HTTPException(404, detail=f"task {task_id!r} not found")
        events = (
            await session.execute(
                select(Event)
                .where(Event.task_id == task_id)
                .order_by(Event.id)
            )
        ).scalars().all()

    return {
        "conversation": {
            "task_id": conv.task_id,
            "target_agent": conv.target_agent,
            "target_generation": conv.target_generation,
            "caller_agent": conv.caller_agent,
            "state": conv.state,
            "cabal_escalated_from": conv.cabal_escalated_from,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "result": conv.result_json,
        },
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "payload": e.payload_json,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


def _snippet(result_json: Any, max_chars: int = 120) -> str | None:
    """Pull a short text snippet from the persisted Task body."""
    try:
        msg = (result_json or {}).get("status", {}).get("message") or {}
        parts = msg.get("parts") or []
        for p in parts:
            if p.get("kind") == "text":
                text = p.get("text", "")
                return text if len(text) <= max_chars else text[: max_chars - 1] + "…"
    except Exception:  # noqa: BLE001
        pass
    return None


# ---- Live event tail (Phase U) ----------------------------------------


@router.get("/events/stream")
async def events_stream(
    request: Request,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """SSE stream of events as they're published by Bookkeeper.

    Connection stays open until the client drops. Events emitted before
    the connection opens are NOT replayed — use GET
    /admin/conversations/{id} for historical events.
    """
    _require_operator_claims(request, _extract_bearer(authorization))
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        raise HTTPException(500, detail="event bus not configured")

    async def gen() -> AsyncIterator[bytes]:
        queue = event_bus.subscribe()
        try:
            # Send a hello frame so a slow client can confirm the connection.
            yield _sse(
                "hello", {"msg": "connected", "subscriber_count": event_bus.subscriber_count()}
            )
            while True:
                event = await queue.get()
                yield _sse(event["event_type"], event)
        except asyncio.CancelledError:
            return
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse(event_name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


# ---- identity hot reload (Phase Y) -----------------------------------


@router.post("/reload/identity")
async def reload_identity(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Re-read identity.toml and rebuild persona LocalHandlers in place.

    In-flight Tasks finish on the OLD handler (we don't touch them).
    Subsequent dispatches go to the freshly built handlers. Bulletin-board
    rows for newly declared personas are inserted; rows for removed
    personas stay (with their now-stale agent_card) — clean removal is a
    separate operation since the rows are still queryable from
    /admin/conversations etc.

    LiteLLMClient stays the SAME instance (preserves breaker + budget
    state). If [litellm] changed, the relevant fields are mutated in
    place.
    """
    from cabal.config.identity import load_identity
    from cabal.core.identity_sync import sync_personas
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.persona_factory import register_personas_from_identity

    _require_operator_claims(request, _extract_bearer(authorization))
    state = request.app.state
    identity_path = getattr(state, "identity_path", None)
    if identity_path is None or not identity_path.exists():
        raise HTTPException(404, detail=f"identity file not found: {identity_path}")

    try:
        identity = load_identity(identity_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, detail=f"identity parse failed: {exc}") from exc

    # 1. Sync bulletin board for any newly-declared personas.
    async with open_session(state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=state.master_key
        )
    if stored is None:
        raise HTTPException(500, detail="no active signing key")
    await sync_personas(state.sessionmaker, identity, stored)

    # 2. If the LiteLLM endpoint or key env changed, update the client in
    # place — don't toss the breaker/budget state.
    litellm_client = getattr(state, "litellm_client", None)
    if litellm_client is not None:
        import os as _os
        litellm_client.api_base = identity.litellm.api_base
        if identity.litellm.api_key_env:
            litellm_client.api_key = _os.environ.get(identity.litellm.api_key_env)

    if litellm_client is None:
        return {
            "reloaded": True,
            "registered": [],
            "warning": "no litellm client; persona handlers were not rebuilt",
        }

    # 3. Rebuild + re-register persona handlers. Re-use the existing MCP
    # http client and signing key — reload should NOT toss the breaker
    # the persona was talking through.
    state.identity = identity
    registered = register_personas_from_identity(
        dispatch_router=state.dispatch_router,
        identity=identity,
        litellm_client=litellm_client,
        memory_store=getattr(state, "memory_store", None) or _make_default_memory_store(),
        session_tracker=getattr(state, "session_tracker", None) or _make_default_session_tracker(),
        hook_registry=state.hook_registry,
        mcp_http_client=getattr(state, "mcp_http_client", None),
        signing_key=getattr(state, "mcp_signing_key", None),
    )
    return {"reloaded": True, "registered": registered}


def _make_default_memory_store():
    from cabal.core.memory.store import NoOpMemoryStore
    return NoOpMemoryStore()


def _make_default_session_tracker():
    from cabal.core.memory.session_tracker import NoOpSessionTracker
    return NoOpSessionTracker()
