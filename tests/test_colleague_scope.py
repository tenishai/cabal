"""The `colleague` scope: directory read + dispatch re-mint, nothing else.

A separate room service mints colleague passes (aud="cabal-core",
sub="agent:<name>", cabal_scope=["colleague"]). This file covers:

  1. GET /directory auth matrix (no token, dispatch-scoped, colleague,
     operator, wrong-audience colleague token).
  2. Serialization tolerance of the bulletin board's `agent_card_json`
     (bare card, jws-wrapped card, missing/None metadata) and the
     ACTIVE-only filter.
  3. Colleague re-mint on POST /a2a/tasks: a colleague token dispatching
     to a registered target gets a fresh per-target token minted for it,
     and its `sub` survives into the resulting Conversation.caller_agent.
  4. Colleague-cannot-admin: the same colleague token is refused by
     /admin/agents, /admin/conversations, and /admin/token/mint-service.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode, Conversation, Persona


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _mint_token(
    app: FastAPI, *, sub: str, aud: str, scope: list[str]
) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud=aud, scope=scope)


async def _colleague_token(app: FastAPI, *, sub: str = "agent:room") -> str:
    return await _mint_token(app, sub=sub, aud="cabal-core", scope=["colleague"])


# ---- 1. directory auth matrix -----------------------------------------


@pytest.mark.asyncio
async def test_directory_no_token_rejected(client: AsyncClient) -> None:
    r = await client.get("/directory")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_directory_dispatch_scope_rejected(app: FastAPI, client: AsyncClient) -> None:
    token = await _mint_token(app, sub="persona:archivist", aud="cabal-core", scope=["dispatch"])
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_directory_colleague_scope_accepted(app: FastAPI, client: AsyncClient) -> None:
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert "agents" in r.json()


@pytest.mark.asyncio
async def test_directory_operator_scope_accepted(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert "agents" in r.json()


@pytest.mark.asyncio
async def test_directory_colleague_wrong_audience_rejected(
    app: FastAPI, client: AsyncClient
) -> None:
    """A colleague-scoped token minted for a different audience (e.g. one
    already aimed at a specific target) must not read the directory."""
    token = await _mint_token(app, sub="agent:room", aud="echo", scope=["colleague"])
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# ---- 2. serialization tolerance + ACTIVE-only filter -------------------


async def _seed_agent(
    app: FastAPI,
    *,
    name: str,
    agent_card_json: dict,
    status: str = "active",
) -> None:
    async with app.state.sessionmaker() as s:
        s.add(Persona(name=name, config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name=name,
            persona_name=name,
            role="singleton",
            endpoint=f"local://{name}",
            capabilities={},
            safety_tier="strict",
            agent_card_json=agent_card_json,
            agent_card_kid=app.state.active_kid,
            status=status,
        )
        await s.commit()


@pytest.mark.asyncio
async def test_directory_reads_bare_card(app: FastAPI, client: AsyncClient) -> None:
    """No {'card': ...} wrapper — the row itself is the card."""
    await _seed_agent(
        app,
        name="bare",
        agent_card_json={
            "name": "bare",
            "description": "a bare-card agent",
            "metadata": {
                "team": "growth",
                "purpose_tags": ["billing"],
                "capability": "invoicing",
            },
        },
    )
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    bare = next(a for a in r.json()["agents"] if a["name"] == "bare")
    assert bare == {
        "name": "bare",
        "team": "growth",
        "purpose_tags": ["billing"],
        "capability": "invoicing",
        "description": "a bare-card agent",
    }


@pytest.mark.asyncio
async def test_directory_reads_jws_wrapped_card(app: FastAPI, client: AsyncClient) -> None:
    """{'jws': ..., 'card': {...}} — the shape sign_card()/primus_rotate produce."""
    await _seed_agent(
        app,
        name="wrapped",
        agent_card_json={
            "jws": "not-a-real-signature",
            "card": {
                "name": "wrapped",
                "description": "a jws-wrapped agent",
                "metadata": {
                    "team": "platform",
                    "purpose_tags": ["infra", "on-call"],
                    "capability": "provisioning",
                },
            },
        },
    )
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    wrapped = next(a for a in r.json()["agents"] if a["name"] == "wrapped")
    assert wrapped == {
        "name": "wrapped",
        "team": "platform",
        "purpose_tags": ["infra", "on-call"],
        "capability": "provisioning",
        "description": "a jws-wrapped agent",
    }


@pytest.mark.asyncio
async def test_directory_missing_metadata_defaults(app: FastAPI, client: AsyncClient) -> None:
    """No metadata block at all (like the built-in echo) — defaults, no crash."""
    await _seed_agent(app, name="sparse", agent_card_json={"name": "sparse"})
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    sparse = next(a for a in r.json()["agents"] if a["name"] == "sparse")
    assert sparse == {
        "name": "sparse",
        "team": "",
        "purpose_tags": [],
        "capability": "",
        "description": "",
    }


@pytest.mark.asyncio
async def test_directory_none_metadata_defaults(app: FastAPI, client: AsyncClient) -> None:
    """metadata explicitly None (not just absent) must not blow up `.get`."""
    await _seed_agent(
        app,
        name="nullmeta",
        agent_card_json={"name": "nullmeta", "metadata": None},
    )
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    row = next(a for a in r.json()["agents"] if a["name"] == "nullmeta")
    assert row["team"] == ""
    assert row["purpose_tags"] == []
    assert row["capability"] == ""


@pytest.mark.asyncio
async def test_directory_includes_builtin_echo(app: FastAPI, client: AsyncClient) -> None:
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    names = {a["name"] for a in r.json()["agents"]}
    assert "echo" in names


@pytest.mark.asyncio
async def test_directory_excludes_retired_row(app: FastAPI, client: AsyncClient) -> None:
    await _seed_agent(
        app, name="gone", agent_card_json={"name": "gone"}, status="retired"
    )
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    names = {a["name"] for a in r.json()["agents"]}
    assert "gone" not in names


@pytest.mark.asyncio
async def test_directory_excludes_draining_row(app: FastAPI, client: AsyncClient) -> None:
    await _seed_agent(
        app, name="draining-one", agent_card_json={"name": "draining-one"}, status="draining"
    )
    token = await _colleague_token(app)
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    names = {a["name"] for a in r.json()["agents"]}
    assert "draining-one" not in names


# ---- 3. colleague re-mint dispatch --------------------------------------


@pytest.mark.asyncio
async def test_colleague_can_dispatch_to_echo(app: FastAPI, client: AsyncClient) -> None:
    """A colleague token (aud=cabal-core) is re-minted per-target, same as
    an operator token, and the dispatch succeeds."""
    token = await _colleague_token(app, sub="agent:room-service")
    body = {
        "target": "echo",
        "task": {
            "id": "t-colleague-1",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hi from a colleague"}],
                },
            },
        },
    }
    resp = await client.post(
        "/a2a/tasks", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["status"]["state"] == "completed"


@pytest.mark.asyncio
async def test_colleague_sub_survives_into_caller_agent(app: FastAPI, client: AsyncClient) -> None:
    """The colleague's `sub` (agent:<name>) passes through the re-mint and
    lands in the Conversation row's caller_agent — pinned at the
    _resolve_dispatch_token level per the sub passthrough contract."""
    token = await _colleague_token(app, sub="agent:room-service")
    body = {
        "target": "echo",
        "task": {
            "id": "t-colleague-2",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "who dispatched me"}],
                },
            },
        },
    }
    resp = await client.post(
        "/a2a/tasks", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text

    async with app.state.sessionmaker() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.task_id == "t-colleague-2"))
        ).scalar_one()
    assert conv.caller_agent == "agent:room-service"


@pytest.mark.asyncio
async def test_colleague_cannot_dispatch_without_scope_match(
    app: FastAPI, client: AsyncClient
) -> None:
    """Sanity check on the other side of the gate: a plain dispatch-scoped
    token aimed at the wrong target still gets 403 (guard parity for the
    non-colleague, non-operator path)."""
    token = await _mint_token(app, sub="persona:archivist", aud="archivist", scope=["dispatch"])
    body = {
        "target": "echo",
        "task": {
            "id": "t-colleague-3",
            "status": {
                "state": "submitted",
                "message": {"role": "user", "parts": [{"kind": "text", "text": "x"}]},
            },
        },
    }
    resp = await client.post(
        "/a2a/tasks", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


# ---- 4. colleague cannot admin ------------------------------------------


@pytest.mark.asyncio
async def test_colleague_cannot_list_admin_agents(app: FastAPI, client: AsyncClient) -> None:
    token = await _colleague_token(app)
    r = await client.get("/admin/agents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_colleague_cannot_list_admin_conversations(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _colleague_token(app)
    r = await client.get("/admin/conversations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_colleague_cannot_mint_service_token(app: FastAPI, client: AsyncClient) -> None:
    token = await _colleague_token(app)
    r = await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:whatever",
            "scope": ["secrets-fetch"],
            "aud": "cabal-core",
            "ttl_days": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
