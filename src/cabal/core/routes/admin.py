"""Operator-only admin routes.

GET /admin/agents — list every row in the bulletin board (active, draining,
retired).

Auth: requires a token with `cabal_scope` containing 'operator'. The
returned listing intentionally includes safety_tier + last_heartbeat so
the operator can spot stuck handlers.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select

from cabal.core.a2a.card import sign_card
from cabal.core.a2a.types import AgentCapabilities, AgentCard, Skill, Task
from cabal.core.auth.jwt import JWTError, extract_bearer, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Agent, Conversation, Event, Persona, TaskCheckpoint

router = APIRouter(prefix="/admin", tags=["admin"])

# Audience constants — must match what bootstrap / mint-service produce.
# Operator tokens: aud="cabal-core" (minted in routes/auth.py bootstrap).
# Liminal service tokens: aud="liminal" (minted by `cbl token mint-service`).
_AUD_OPERATOR = "cabal-core"

#: FALLBACK ONLY (cabal#187). The gateway audience is deployment policy, not a
#: constant: an operator whose MCP gateway answers to some other audience sets
#: `[liminal] jwt_aud`. Read it through `_gateway_audience`, never directly —
#: this name exists for the window where no identity is loaded at all.
_AUD_LIMINAL = "liminal"


def _gateway_audience(request: Request) -> str:
    """The audience this deployment expects on a gateway service token.

    `app.state.identity` is None when cabal is built without a config file
    (app.py:489, and every test app), so the fallback is load-bearing rather
    than decorative. `IdentityConfig.liminal` always exists once identity does
    — it is a `default_factory` field — so only the outer None needs guarding.
    """
    identity = getattr(request.app.state, "identity", None)
    if identity is None:
        return _AUD_LIMINAL
    return identity.liminal.jwt_aud or _AUD_LIMINAL


def _require_operator_claims(request: Request, raw_token: str) -> dict[str, Any]:
    try:
        claims = verify(
            raw_token,
            key_set=request.app.state.jwks_provider(),
            expected_audience=_AUD_OPERATOR,
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
    _require_operator_claims(request, extract_bearer(authorization))
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
                "kind": r.kind,
                "work": r.work,
                "kid": r.agent_card_kid,
                "last_heartbeat": r.last_heartbeat.isoformat() if r.last_heartbeat else None,
                "drain_started_at": r.drain_started_at.isoformat() if r.drain_started_at else None,
            }
            for r in rows
        ]
    }


class SessionRegisterRequest(BaseModel):
    """cabal#142 — a live agent session announcing itself as present.

    `work` is free-form so the shape can settle in use; the CLI renders
    `repo`, `branch` and `task` when present and ignores the rest.
    """

    name: str
    work: dict[str, Any] | None = None


class SessionHeartbeatRequest(BaseModel):
    work: dict[str, Any] | None = None


@router.post("/sessions/register")
async def register_session(
    body: SessionRegisterRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Register a live session as presence-only (cabal#142).

    Idempotent by name: re-registering an already-active session heartbeats it
    instead of failing. A session that reconnects after a crash must not have to
    know whether its previous row survived.
    """
    _require_operator_claims(request, extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        existing = await bulletin_store.get_active(session, body.name)
        if existing is not None:
            await bulletin_store.heartbeat(
                session, body.name, existing.generation, work=body.work
            )
            row = await bulletin_store.get_active(session, body.name)
        else:
            row = await bulletin_store.register_session(
                session, name=body.name, work=body.work
            )
    assert row is not None
    return {"name": row.name, "generation": row.generation, "kind": row.kind}


@router.post("/sessions/{name}/heartbeat")
async def heartbeat_session(
    name: str,
    body: SessionHeartbeatRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator_claims(request, extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        row = await bulletin_store.get_active(session, name)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no active session {name!r}")
        await bulletin_store.heartbeat(session, name, row.generation, work=body.work)
    return {"name": name, "ok": True}


@router.post("/sessions/{name}/end")
async def end_session(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """De-register on clean shutdown.

    Returns ok for an unknown name rather than 404: this is called from a Stop
    hook, and a hook that errors on an already-gone session turns ordinary
    teardown into a visible failure.
    """
    _require_operator_claims(request, extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        row = await bulletin_store.get_active(session, name)
        if row is not None:
            await bulletin_store.mark_retired(session, name, row.generation)
    return {"name": name, "ok": True}


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
    claims = _require_operator_claims(request, extract_bearer(authorization))
    agent_name = req.persona if req.persona.endswith("-primus") else f"{req.persona}-primus"

    async with open_session(request.app.state.sessionmaker) as session:
        # 1. Find the current active row
        existing = await session.execute(
            select(Agent).where(Agent.name == agent_name, Agent.status == "active")
        )
        current = existing.scalar_one_or_none()
        if current is None:
            raise HTTPException(404, detail=f"no active row for primus {agent_name!r}")

        # 2. Mint a fresh signed AgentCard for the new generation
        stored = await load_active_signing_key(session, master_key=request.app.state.master_key)
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
        current.drain_started_at = datetime.now(UTC)
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


# ---- Persona lifecycle: graduate + absorb (cabal#45) ------------------


class PersonaGraduateRequest(BaseModel):
    endpoint: str
    audience: str
    agent_card_json: dict = {}
    agent_card_kid: str = ""


@router.post("/persona/{name}/graduate")
async def persona_graduate(
    name: str,
    req: PersonaGraduateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Graduate a local persona to a federated sovereign peer (cabal#45).

    Retires the existing active row for `name` (if any), registers a
    `kind="federated"` row pointing at the sovereign service's endpoint, and
    unregisters the in-process local handler so subsequent dispatches route
    remotely.

    The caller supplies the sovereign service's stable service id as
    `audience` — this is what callers must mint into their dispatch token
    `aud`. `agent_card_json`/`agent_card_kid` are the sovereign service's
    own signed AgentCard; pass empty values if the service has not yet issued
    one (they can be updated via a future operation).
    """
    _require_operator_claims(request, extract_bearer(authorization))

    if not req.endpoint or not req.audience:
        raise HTTPException(400, detail="endpoint and audience must be non-empty")

    dispatch_router = request.app.state.dispatch_router
    sessionmaker = request.app.state.sessionmaker

    async with open_session(sessionmaker) as session:
        # Retire any existing active row for this name.
        existing = await bulletin_store.get_active(session, name)
        if existing is not None:
            await bulletin_store.mark_retired(session, name, existing.generation)
        # Derive from MAX(generation) across ALL statuses — not the active row —
        # so a prior graduate→absorb (which retires without a successor) can't
        # collide on the (name, generation) PK (cabal#45).
        next_gen = await bulletin_store.next_generation(session, name)

        # `agents.persona_name` FKs to `personas.name` (PRAGMA foreign_keys=ON),
        # so the persona row must exist before register_federated inserts. A
        # client (non-parent) cabal graduating a name it never owned locally has
        # no Persona row — upsert an empty one in this same transaction so the
        # federated insert doesn't FK-fail (federated-bulletin-and-router §3).
        persona_name = existing.persona_name if existing is not None else name
        if await session.get(Persona, persona_name) is None:
            session.add(Persona(name=persona_name, config_json={}))
            await session.flush()

        # Register the federated row.
        federated_row = await bulletin_store.register_federated(
            session,
            name=name,
            persona_name=persona_name,
            endpoint=req.endpoint,
            federation_audience=req.audience,
            agent_card_json=req.agent_card_json,
            agent_card_kid=req.agent_card_kid,
            generation=next_gen,
        )

    # Drop the local handler so dispatch routes remote (belt + suspenders;
    # the dispatch-time federated guard is the primary enforcement).
    dispatch_router.unregister_local(name)

    return {
        "graduated": True,
        "agent": name,
        "generation": federated_row.generation,
        "endpoint": req.endpoint,
        "audience": req.audience,
        "previous_generation_retired": existing.generation if existing is not None else None,
    }


@router.post("/persona/{name}/absorb")
async def persona_absorb(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Re-absorb a federated persona back into this cabal as a local member.

    Retires the active `kind="federated"` row for `name`. The local handler
    and `member` bulletin row are restored on the next identity reload
    (`cbl reload identity`), since only the persona factory knows the correct
    endpoint/card for the local instance.

    Raises 404 if `name` has no active federated row.
    Raises 409 if the active row is not federated (already local; no-op
    needed).
    """
    _require_operator_claims(request, extract_bearer(authorization))

    sessionmaker = request.app.state.sessionmaker
    async with open_session(sessionmaker) as session:
        row = await bulletin_store.get_active(session, name)
        if row is None:
            raise HTTPException(404, detail=f"no active row for agent {name!r}")
        if row.kind != "federated":
            raise HTTPException(
                409,
                detail=(
                    f"agent {name!r} is not federated (kind={row.kind!r}); "
                    "absorb is only meaningful for federated personas"
                ),
            )
        await bulletin_store.mark_retired(session, name, row.generation)

    return {
        "absorbed": True,
        "agent": name,
        "retired_generation": row.generation,
        "note": (
            "run `cbl reload identity` to restore the local handler "
            "and member bulletin row"
        ),
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
    _require_operator_claims(request, extract_bearer(authorization))
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
    _require_operator_claims(request, extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        conv = (
            await session.execute(select(Conversation).where(Conversation.task_id == task_id))
        ).scalar_one_or_none()
        if conv is None:
            raise HTTPException(404, detail=f"task {task_id!r} not found")
        events = (
            (
                await session.execute(
                    select(Event).where(Event.task_id == task_id).order_by(Event.id)
                )
            )
            .scalars()
            .all()
        )

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


@router.post("/tasks/{task_id}/resume")
async def resume_task(
    request: Request,
    task_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """cabal#2 — replay a checkpointed Task on its persona, continuing
    from the latest saved step. Operator-only. Returns the resumed Task
    once it completes.

    Looks up:
      1. The Conversation row to identify the target agent.
      2. The task_checkpoints row to load the saved messages + step_index.
      3. The in-process LocalHandler (resume requires local persona).

    Drops the checkpoint on successful completion (handle_task does it).
    """
    _require_operator_claims(request, extract_bearer(authorization))

    sessionmaker = request.app.state.sessionmaker
    async with open_session(sessionmaker) as session:
        conv = (
            await session.execute(select(Conversation).where(Conversation.task_id == task_id))
        ).scalar_one_or_none()
        if conv is None:
            raise HTTPException(404, detail=f"task {task_id!r} not found")
        ckpt_row = (
            await session.execute(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id))
        ).scalar_one_or_none()

    if ckpt_row is None:
        raise HTTPException(
            404,
            detail=(
                f"no checkpoint for task {task_id!r} — either the persona "
                "did not opt into checkpointing or the Task already completed"
            ),
        )
    if not ckpt_row.task_json:
        raise HTTPException(
            500,
            detail=f"checkpoint for {task_id!r} is missing task_json; cannot resume",
        )

    dispatch_router = request.app.state.dispatch_router
    handler = dispatch_router.get_local(conv.target_agent)
    if handler is None:
        raise HTTPException(
            503,
            detail=(
                f"agent {conv.target_agent!r} has no in-process handler; "
                "resume is only supported for local personas"
            ),
        )

    checkpoint_store = request.app.state.checkpoint_store
    checkpoint = await checkpoint_store.load(task_id)
    if checkpoint is None:
        # Race: checkpoint dropped between the select above and load.
        raise HTTPException(
            409,
            detail=f"checkpoint for {task_id!r} vanished before resume could load it",
        )

    try:
        task = Task.model_validate(ckpt_row.task_json)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, detail=f"checkpoint task_json failed to parse: {exc}") from exc

    response = await dispatch_router.resume_local(
        target_agent=conv.target_agent,
        task=task,
        resume_from=checkpoint,
    )

    return {
        "task_id": task_id,
        "state": response.status.state,
        "result": response.model_dump(by_alias=True, exclude_none=True),
    }


@router.post("/tasks/{task_id}/abort")
async def abort_task(
    request: Request,
    task_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """cabal#11 — operator cancellation. Aborts the in-flight task and every
    descendant (subordinate dispatches, nested LLM/tool calls) via the
    AbortGraph. Cooperative: running loops stop at their next safe boundary,
    leaving a resumable checkpoint. Returns the number of nodes signalled
    (0 if the task is not currently in-flight)."""
    _require_operator_claims(request, extract_bearer(authorization))
    coordinator = request.app.state.dispatch_router.abort_coordinator
    signalled = coordinator.abort(task_id, reason="operator")
    return {"task_id": task_id, "signalled": signalled}


@router.get("/tasks/{task_id}/graph")
async def task_graph(
    request: Request,
    task_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """cabal#11 — live AbortGraph subtree for a task: the tree of in-flight
    invocations rooted at `task_id`. Empty `tree` means nothing is running
    under that id right now."""
    _require_operator_claims(request, extract_bearer(authorization))
    coordinator = request.app.state.dispatch_router.abort_coordinator
    return {"task_id": task_id, "tree": coordinator.dump(task_id)}


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
    _require_operator_claims(request, extract_bearer(authorization))
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
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode()


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
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.identity_sync import sync_personas
    from cabal.core.persona_factory import register_personas_from_identity

    _require_operator_claims(request, extract_bearer(authorization))
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
        stored = await load_active_signing_key(session, master_key=state.master_key)
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


# ---- secrets endpoint for liminal (Phase GG) ------------------------


@router.get("/secrets/{persona}/{handler_name}")
async def get_secrets(
    persona: str,
    handler_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return the credential set for `(persona, handler_name)`.

    Two sources, in order of precedence:
      1. DB-backed encrypted secrets, decrypted with the daemon's
         master_key. Persisted via `cbl secret set` — hot-updatable
         without daemon restart.
      2. Env vars `CABAL_SECRET_<persona>_<handler>_<KEY>` — override
         the DB so an operator can paste a one-off value into a shell
         without disturbing persisted state.

    Auth: operator-scope OR a token scoped specifically for
    `secrets-fetch`. The operator gate is fine for solo; team-scale
    deployments will mint a dedicated liminal-service token with
    `secrets-fetch` scope so the operator isn't typing operator JWTs
    on every liminal restart.
    """
    import os as _os
    import re

    from cabal.core.secrets import merge_with_env

    claims = _require_secrets_fetch(request, extract_bearer(authorization))
    persona_norm = _normalize(persona)
    handler_norm = _normalize(handler_name)
    if not persona_norm or not handler_norm:
        raise HTTPException(400, detail="persona and handler must be non-empty")

    # 1) Backend-stored (Fernet-encrypted SQLite by default).
    db_creds = await request.app.state.secrets_backend.get(
        persona=persona_norm, handler=handler_norm
    )

    # 2) Env vars override.
    prefix = f"CABAL_SECRET_{persona_norm}_{handler_norm}_"
    pat = re.compile(f"^{re.escape(prefix)}", re.IGNORECASE)
    env_pairs: list[tuple[str, str]] = []
    for env_key, value in _os.environ.items():
        m = pat.match(env_key)
        if m is None:
            continue
        local_key = env_key[m.end() :]
        if local_key:
            env_pairs.append((local_key, value))

    creds = merge_with_env(db_values=db_creds, env_values=env_pairs)

    return {
        "persona": persona_norm,
        "handler": handler_norm,
        "credentials": creds,
        "caller": claims.get("sub"),
        "sources": {"db": len(db_creds), "env": len(env_pairs)},
    }


# ---- secret write/list/unset (Phase KK) -----------------------------


class SecretSetRequest(BaseModel):
    key: str
    value: str
    purpose: str | None = None


@router.post("/secrets/{persona}/{handler_name}")
async def set_secret(
    persona: str,
    handler_name: str,
    req: SecretSetRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Store an encrypted secret. Operator-scope only.

    KEY must look like an env-var (upper-snake): handler authors document
    the keys they expect in their capability_card.credential_keys, so
    consistency matters."""
    import re

    _require_operator_claims(request, extract_bearer(authorization))
    persona_norm = _normalize(persona)
    handler_norm = _normalize(handler_name)
    if not req.key or not req.value:
        raise HTTPException(400, detail="key and value must be non-empty")
    if not re.match(r"^[A-Z][A-Z0-9_]*$", req.key):
        raise HTTPException(
            400,
            detail=("key must match ^[A-Z][A-Z0-9_]*$ (upper-snake to match env-var convention)"),
        )

    await request.app.state.secrets_backend.set(
        persona=persona_norm,
        handler=handler_norm,
        key=req.key,
        value=req.value,
        purpose=req.purpose,
    )

    return {
        "persona": persona_norm,
        "handler": handler_norm,
        "key": req.key,
        "stored": True,
    }


@router.delete("/secrets/{persona}/{handler_name}/{key}")
async def unset_secret(
    persona: str,
    handler_name: str,
    key: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator_claims(request, extract_bearer(authorization))
    persona_norm = _normalize(persona)
    handler_norm = _normalize(handler_name)
    if not key:
        raise HTTPException(400, detail="key must be non-empty")

    existed = await request.app.state.secrets_backend.unset(
        persona=persona_norm,
        handler=handler_norm,
        key=key,
    )
    return {
        "persona": persona_norm,
        "handler": handler_norm,
        "key": key,
        "deleted": existed,
    }


@router.get("/secrets")
async def list_secrets(
    request: Request,
    authorization: str | None = Header(default=None),
    persona: str | None = None,
    handler: str | None = None,
) -> dict[str, Any]:
    """List secret NAMES (persona, handler, key tuples) — never values.
    Operator scope required."""
    _require_operator_claims(request, extract_bearer(authorization))
    persona_norm = _normalize(persona) if persona is not None else None
    handler_norm = _normalize(handler) if handler is not None else None

    entries = await request.app.state.secrets_backend.list_entries(
        persona=persona_norm, handler=handler_norm
    )
    return {
        "entries": [{"persona": e.persona, "handler": e.handler, "key": e.key} for e in entries]
    }


def _normalize(s: str) -> str:
    """Strip and reject obvious injection (only word chars + hyphens)."""
    import re

    s = s.strip()
    if not re.match(r"^[A-Za-z0-9_-]+$", s):
        raise HTTPException(
            400,
            detail=(
                f"name {s!r} contains characters not in [A-Za-z0-9_-]; "
                "refuse to translate to env-var prefix"
            ),
        )
    return s


def _require_secrets_fetch(request: Request, raw_token: str) -> dict[str, Any]:
    """Like _require_operator_claims, but also accepts tokens whose scope
    contains 'secrets-fetch' (the dedicated liminal service scope).

    Accepted audiences:
      - "cabal-core" — operator token (full operator access)
      - the gateway audience (`[liminal] jwt_aud`, default "liminal") —
        gateway service token, secrets-fetch scope only
    A token minted for any other audience is rejected regardless of scope.
    """
    try:
        claims = verify(
            raw_token,
            key_set=request.app.state.jwks_provider(),
            expected_audience=None,  # we check manually below (two valid audiences)
        )
    except JWTError as exc:
        raise HTTPException(401, detail=str(exc)) from exc

    aud = claims.get("aud")
    if aud not in {_AUD_OPERATOR, _gateway_audience(request)}:
        raise HTTPException(401, detail="invalid_audience")

    scope = claims.get("cabal_scope") or []
    if "operator" not in scope and "secrets-fetch" not in scope:
        raise HTTPException(
            403,
            detail="requires operator OR secrets-fetch scope",
        )
    return claims


# ---- service token mint (Phase II) ------------------------------------


class MintServiceRequest(BaseModel):
    sub: str
    scope: list[str]
    aud: str
    ttl_days: int
    persona: str | None = None

    def ttl_seconds(self) -> int:
        # Cap at 365 days to avoid silly-long lifetimes.
        return max(1, min(self.ttl_days, 365)) * 24 * 60 * 60


@router.post("/token/mint-service")
async def mint_service_token(
    req: MintServiceRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Operator-only: mint a long-lived JWT for a peer service.

    The canonical use case is `cbl token mint-service --scope secrets-fetch`
    which the operator copies into liminal's `LIMINAL_CABAL_TOKEN` env so
    liminal can fetch credentials from `GET /admin/secrets/{persona}/{handler}`.

    Service tokens are operator-rotated; we don't track them in a DB or
    revocation list. Operator restarts the daemon with a new signing key
    to invalidate everything signed under the old `kid`.
    """
    from datetime import datetime, timedelta

    _require_operator_claims(request, extract_bearer(authorization))

    if not req.sub or not req.scope or not req.aud:
        raise HTTPException(400, detail="sub, scope, and aud must all be non-empty")

    async with open_session(request.app.state.sessionmaker) as session:
        from cabal.core.auth.keys import load_active_signing_key

        stored = await load_active_signing_key(session, master_key=request.app.state.master_key)
    if stored is None:
        raise HTTPException(500, detail="no active signing key")

    from cabal.core.auth.jwt import mint as _mint

    ttl = req.ttl_seconds()
    token = _mint(
        signing_key=stored,
        sub=req.sub,
        aud=req.aud,
        persona=req.persona,
        scope=req.scope,
        ttl_seconds=ttl,
    )
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
    return {
        "access_token": token,
        "sub": req.sub,
        "scope": req.scope,
        "aud": req.aud,
        "expires_at": expires_at,
        "kid": stored.kid,
    }
