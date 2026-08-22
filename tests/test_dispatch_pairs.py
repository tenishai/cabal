"""Dispatch pairs: the switchboard checks who may call whom.

kobo (the room) owns the policy of which agent may dispatch to which
target; cabal just stores flat `(caller, target)` pairs and enforces
them at the re-mint step in `POST /a2a/tasks` (see
`routes/tasks.py::_resolve_dispatch_token`).

An EMPTY table means OPEN: before the room has ever synced a set of
pairs, every agent-to-agent dispatch is allowed. Enforcement begins the
instant the first (possibly empty-again) sync lands via
`PUT /admin/dispatch-pairs` (CABAL-DECISIONS.md D-017).

Covers:
  1. Enforcement matrix on POST /a2a/tasks: pair allowed, pair missing
     (403), empty table (open), operator caller (never gated), and a
     non-agent service sub (unaffected, pre-existing 403 behavior).
  2. GET/PUT /admin/dispatch-pairs: auth required, atomic replace proven
     by an interleaved read, shape validation (422), and the sanity cap.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode

TARGET = "echo"


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _mint_token(
    app: FastAPI, *, sub: str, aud: str = "cabal-core", scope: list[str] | None = None
) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud=aud, scope=scope or [])


def _task_body(target: str, task_id: str) -> dict:
    return {
        "target": target,
        "task": {
            "id": task_id,
            "status": {
                "state": "submitted",
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            },
        },
    }


async def _dispatch(client: AsyncClient, token: str, *, target: str = TARGET, task_id: str = "t-1"):
    return await client.post(
        "/a2a/tasks",
        json=_task_body(target, task_id),
        headers={"Authorization": f"Bearer {token}"},
    )


# ---- 1. enforcement matrix on POST /a2a/tasks --------------------------


@pytest.mark.asyncio
async def test_empty_table_is_open(app: FastAPI, client: AsyncClient) -> None:
    """No pairs have ever been synced: every agent may dispatch anywhere."""
    token = await _mint_token(app, sub="agent:room")
    resp = await _dispatch(client, token, task_id="t-open")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_allowed_pair_dispatches(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])

    token = await _mint_token(app, sub="agent:sizing-agent")
    resp = await _dispatch(client, token, task_id="t-allowed")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_missing_pair_is_403_with_readable_body(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    # Non-empty table that does NOT mention agent:rogue -> rogue is denied.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])

    token = await _mint_token(app, sub="agent:rogue")
    resp = await _dispatch(client, token, task_id="t-missing")
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "agent:rogue" in detail
    assert TARGET in detail
    assert "not a colleague" in detail
    assert "\u2014" not in detail  # no em dashes in user-facing copy


@pytest.mark.asyncio
async def test_operator_caller_never_gated(app: FastAPI, client: AsyncClient) -> None:
    """An operator token dispatches regardless of dispatch_pairs content."""
    from cabal.core import dispatch_pairs

    # Non-empty table that mentions nobody relevant.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:someone-else", "someone-else-target")])

    token = await _operator_token(app, client)
    resp = await _dispatch(client, token, task_id="t-operator")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_non_agent_service_sub_unaffected_by_pairs(app: FastAPI, client: AsyncClient) -> None:
    """A non-agent sub (e.g. a persona service token) never consults
    dispatch_pairs; it keeps the pre-existing aud-mismatch 403, whether the
    table is empty or populated."""
    from cabal.core import dispatch_pairs

    # Even with an EMPTY table (which is "open" for agent: subs), a
    # non-agent sub aimed at the wrong audience must still be rejected.
    token = await _mint_token(app, sub="persona:archivist", aud="archivist", scope=["dispatch"])
    resp = await _dispatch(client, token, task_id="t-service-1")
    assert resp.status_code == 403

    # And with a populated table that would otherwise gate agent: subs,
    # the non-agent sub's rejection reason is unchanged (not a pairs 403).
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])
    resp = await _dispatch(client, token, task_id="t-service-2")
    assert resp.status_code == 403
    assert "not a colleague" not in resp.json()["detail"]


# ---- 2. PUT/GET /admin/dispatch-pairs ----------------------------------


@pytest.mark.asyncio
async def test_get_dispatch_pairs_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/admin/dispatch-pairs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_dispatch_pairs_requires_auth(client: AsyncClient) -> None:
    resp = await client.put("/admin/dispatch-pairs", json={"pairs": []})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_dispatch_pairs_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    token = await _mint_token(app, sub="persona:archivist", scope=["dispatch"])
    resp = await client.get("/admin/dispatch-pairs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_dispatch_pairs_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    token = await _mint_token(app, sub="agent:room", scope=["dispatch"])
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_then_get_round_trips(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    put_resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"], ["agent:c", "d"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert put_resp.status_code == 200, put_resp.text

    get_resp = await client.get(
        "/admin/dispatch-pairs", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200, get_resp.text
    pairs = {tuple(p) for p in get_resp.json()["pairs"]}
    assert pairs == {("agent:a", "b"), ("agent:c", "d")}


@pytest.mark.asyncio
async def test_put_replaces_whole_set(app: FastAPI, client: AsyncClient) -> None:
    """A second PUT fully replaces the first -- no merge, no leftovers."""
    token = await _operator_token(app, client)
    await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:x", "y"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    get_resp = await client.get(
        "/admin/dispatch-pairs", headers={"Authorization": f"Bearer {token}"}
    )
    pairs = {tuple(p) for p in get_resp.json()["pairs"]}
    assert pairs == {("agent:x", "y")}


@pytest.mark.asyncio
async def test_put_rejects_malshaped_pairs(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    # A 3-element "pair" is not a (str, str) pair.
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b", "extra"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_rejects_non_string_elements(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", 5]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_rejects_over_cap(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    token = await _operator_token(app, client)
    too_many = [[f"agent:{i}", "t"] for i in range(dispatch_pairs.MAX_PAIRS + 1)]
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": too_many},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_replace_all_is_atomic_under_interleaved_read(app: FastAPI) -> None:
    """The write is DELETE-all + INSERT within a single uncommitted
    transaction. A concurrent reader on a separate session must never
    observe the dangerous half-state (old rows deleted, new rows not yet
    inserted) -- that momentary "empty" would be misread as OPEN by
    `is_allowed`. Proven here by literally interleaving a paused writer
    with a reader on another connection."""
    from cabal.core import dispatch_pairs

    # Seed an initial, non-empty set.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:old", "target-old")])

    writer_started = asyncio.Event()
    writer_may_commit = asyncio.Event()
    writer_session = app.state.sessionmaker()

    async def writer() -> None:
        await dispatch_pairs.replace_all(writer_session, [("agent:new", "target-new")])
        writer_started.set()
        await writer_may_commit.wait()
        await writer_session.commit()
        await writer_session.close()

    writer_task = asyncio.create_task(writer())
    await writer_started.wait()

    # The writer has deleted the old row and staged the new one, but has
    # NOT committed. A fresh reader session must still see the OLD,
    # fully-committed state -- never an empty table.
    async with open_session(app.state.sessionmaker) as session:
        pairs = await dispatch_pairs.list_all(session)
    assert pairs == [("agent:old", "target-old")], (
        "reader observed a half-written state; the replace is not atomic"
    )

    writer_may_commit.set()
    await writer_task

    async with open_session(app.state.sessionmaker) as session:
        pairs = await dispatch_pairs.list_all(session)
    assert pairs == [("agent:new", "target-new")]
