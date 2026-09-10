"""/admin/sessions routes — operator-visible presence for live agent sessions (cabal#142)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.models import BootstrapCode


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        result = await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        code = result.scalar_one().code
    r = await client.post("/auth/bootstrap", json={"code": code})
    return r.json()["access_token"]


def _auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.asyncio
async def test_a_registered_session_shows_up_in_the_roster(app, client) -> None:
    """The whole point of the issue: `cbl status` answers 'what is running'."""
    tok = await _operator_token(app, client)
    r = await client.post(
        "/admin/sessions/register",
        json={"name": "poe2", "work": {"repo": "liminal", "task": "hardening bill"}},
        headers=_auth(tok),
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "session"

    listing = await client.get("/admin/agents", headers=_auth(tok))
    rows = {a["name"]: a for a in listing.json()["agents"]}
    assert "poe2" in rows
    assert rows["poe2"]["kind"] == "session"
    assert rows["poe2"]["work"]["task"] == "hardening bill"
    # and it sits beside the personas rather than replacing them
    assert "echo" in rows
    assert rows["echo"]["kind"] == "member"


@pytest.mark.asyncio
async def test_re_registering_an_active_session_is_idempotent(app, client) -> None:
    """A session reconnecting after a crash must not need to know whether its
    previous row survived. Without this it would 500 on a duplicate PK."""
    tok = await _operator_token(app, client)
    first = await client.post(
        "/admin/sessions/register", json={"name": "poe2"}, headers=_auth(tok)
    )
    second = await client.post(
        "/admin/sessions/register",
        json={"name": "poe2", "work": {"task": "resumed"}},
        headers=_auth(tok),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["generation"] == first.json()["generation"]

    listing = await client.get("/admin/agents", headers=_auth(tok))
    poes = [a for a in listing.json()["agents"] if a["name"] == "poe2"]
    assert len(poes) == 1
    assert poes[0]["work"] == {"task": "resumed"}


@pytest.mark.asyncio
async def test_register_after_end_is_the_daily_cycle_and_must_not_500(
    app, client
) -> None:
    """THE CRITICAL FROM REVIEW, through the routes.

    `end` retires rather than deletes, and CABAL_SESSION_NAME is stable by
    design, so this sequence runs every time a session restarts. It used to
    raise UNIQUE constraint failed: agents.name, agents.generation -- and the
    hook swallowed the 500, so the session just silently stopped appearing.
    """
    tok = await _operator_token(app, client)
    await client.post("/admin/sessions/register", json={"name": "poe2"}, headers=_auth(tok))
    await client.post("/admin/sessions/poe2/end", headers=_auth(tok))

    again = await client.post(
        "/admin/sessions/register",
        json={"name": "poe2", "work": {"task": "day two"}},
        headers=_auth(tok),
    )
    assert again.status_code == 200, again.text
    assert again.json()["generation"] == 2

    listing = await client.get("/admin/agents", headers=_auth(tok))
    live = [
        a for a in listing.json()["agents"]
        if a["name"] == "poe2" and a["status"] == "active"
    ]
    assert len(live) == 1
    assert live[0]["work"] == {"task": "day two"}


@pytest.mark.asyncio
async def test_the_retired_row_survives_the_restart(app, client) -> None:
    """Re-registering must not erase the history -- the retired generation is
    still there, which is the point of retiring rather than deleting."""
    tok = await _operator_token(app, client)
    await client.post("/admin/sessions/register", json={"name": "poe2"}, headers=_auth(tok))
    await client.post("/admin/sessions/poe2/end", headers=_auth(tok))
    await client.post("/admin/sessions/register", json={"name": "poe2"}, headers=_auth(tok))

    listing = await client.get("/admin/agents", headers=_auth(tok))
    gens = sorted(a["generation"] for a in listing.json()["agents"] if a["name"] == "poe2")
    assert gens == [1, 2]


@pytest.mark.asyncio
async def test_heartbeat_updates_the_work_line(app, client) -> None:
    tok = await _operator_token(app, client)
    await client.post("/admin/sessions/register", json={"name": "poe2"}, headers=_auth(tok))
    r = await client.post(
        "/admin/sessions/poe2/heartbeat",
        json={"work": {"repo": "cabal", "branch": "feat/142", "task": "presence rows"}},
        headers=_auth(tok),
    )
    assert r.status_code == 200

    listing = await client.get("/admin/agents", headers=_auth(tok))
    row = next(a for a in listing.json()["agents"] if a["name"] == "poe2")
    assert row["work"]["branch"] == "feat/142"
    assert row["last_heartbeat"] is not None


@pytest.mark.asyncio
async def test_heartbeat_for_an_unknown_session_is_404(app, client) -> None:
    tok = await _operator_token(app, client)
    r = await client.post(
        "/admin/sessions/ghost/heartbeat", json={"work": {}}, headers=_auth(tok)
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_end_retires_the_session(app, client) -> None:
    tok = await _operator_token(app, client)
    await client.post("/admin/sessions/register", json={"name": "poe2"}, headers=_auth(tok))
    r = await client.post("/admin/sessions/poe2/end", headers=_auth(tok))
    assert r.status_code == 200

    listing = await client.get("/admin/agents", headers=_auth(tok))
    row = next(a for a in listing.json()["agents"] if a["name"] == "poe2")
    assert row["status"] != "active"


@pytest.mark.asyncio
async def test_ending_an_unknown_session_is_not_an_error(app, client) -> None:
    """Called from a Stop hook. A hook that errors on an already-gone session
    turns ordinary teardown into a visible failure the operator has to read."""
    tok = await _operator_token(app, client)
    r = await client.post("/admin/sessions/ghost/end", headers=_auth(tok))
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_session_routes_require_operator_scope(app, client) -> None:
    """A persona token must not be able to plant rows in the operator's roster."""
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    persona_token = mint(
        signing_key=stored, sub="persona:archivist", aud="cabal-core", scope=["dispatch"]
    )
    for method, path, payload in (
        ("post", "/admin/sessions/register", {"name": "x"}),
        ("post", "/admin/sessions/x/heartbeat", {"work": {}}),
        ("post", "/admin/sessions/x/end", None),
    ):
        r = await getattr(client, method)(
            path, json=payload, headers=_auth(persona_token)
        )
        assert r.status_code == 403, f"{path} allowed a persona token"


@pytest.mark.asyncio
async def test_session_routes_reject_unauthenticated(client) -> None:
    r = await client.post("/admin/sessions/register", json={"name": "x"})
    assert r.status_code == 401
