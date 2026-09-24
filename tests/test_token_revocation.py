"""Revoking every token a subject holds (cabal#?): the room re-mints an agent's
colleague pass on every re-wire and never took the old one back, so each
rewire left one more live 365-day credential. `POST /admin/token/revoke`
records "tokens for `sub` issued before T are invalid"; every verification
site on the dispatch path and the scope gate refuses such a token from then
on, and a token minted after T passes.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.auth.revocations import RevocationList
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode

SUB = "agent:room-service"


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _colleague_token(app: FastAPI, *, sub: str = SUB, now: int | None = None) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud="cabal-core", scope=["colleague"], now=now)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _task(task_id: str) -> dict:
    return {
        "target": "echo",
        "task": {
            "id": task_id,
            "status": {
                "state": "submitted",
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            },
        },
    }


def _earlier() -> int:
    """An issue time comfortably before "now" and comfortably inside the
    default TTL, so the token is valid on its own terms and only the
    revocation can refuse it."""
    return int(time.time()) - 120


async def _revoke(app, client, sub: str, *, op: str | None = None, **extra) -> object:
    # The bootstrap code is single-use, so a test that revokes twice passes
    # the operator token it already holds.
    op = op or await _operator_token(app, client)
    return await client.post("/admin/token/revoke", json={"sub": sub, **extra}, headers=_bearer(op))


@pytest.mark.asyncio
async def test_a_token_issued_before_the_revocation_is_refused_on_dispatch(
    app: FastAPI, client: AsyncClient
) -> None:
    old = await _colleague_token(app, now=_earlier())
    ok = await client.post("/a2a/tasks", json=_task("t-before"), headers=_bearer(old))
    assert ok.status_code == 200, ok.text

    r = await _revoke(app, client, SUB)
    assert r.status_code == 200
    assert r.json()["sub"] == SUB
    assert isinstance(r.json()["not_before"], int)

    refused = await client.post("/a2a/tasks", json=_task("t-after"), headers=_bearer(old))
    assert refused.status_code == 401
    assert "revoked" in refused.text


@pytest.mark.asyncio
async def test_a_token_issued_after_the_revocation_passes(
    app: FastAPI, client: AsyncClient
) -> None:
    r = await _revoke(app, client, SUB, not_before=1_500_000_000)
    assert r.status_code == 200
    fresh = await _colleague_token(app)  # iat is now, well after the cutoff
    resp = await client.post("/a2a/tasks", json=_task("t-fresh"), headers=_bearer(fresh))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_scope_gate_refuses_a_revoked_token_too(
    app: FastAPI, client: AsyncClient
) -> None:
    old = await _colleague_token(app, now=_earlier())
    assert (await client.get("/directory", headers=_bearer(old))).status_code == 200
    await _revoke(app, client, SUB)
    r = await client.get("/directory", headers=_bearer(old))
    assert r.status_code == 401
    assert "revoked" in r.text


@pytest.mark.asyncio
async def test_json_rpc_refuses_a_revoked_token(app: FastAPI, client: AsyncClient) -> None:
    old = await _colleague_token(app, now=_earlier())
    await _revoke(app, client, SUB)
    envelope = {
        "jsonrpc": "2.0",
        "id": "revoked-rpc-1",
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    }
    r = await client.post("/agents/echo", json=envelope, headers=_bearer(old))
    # JSON-RPC answers 200 and carries the refusal in the envelope's error.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is not None, body
    assert "revoked" in body["error"]["message"]


@pytest.mark.asyncio
async def test_other_subjects_are_untouched(app: FastAPI, client: AsyncClient) -> None:
    other = await _colleague_token(app, sub="agent:other", now=_earlier())
    await _revoke(app, client, SUB)
    resp = await client.post("/a2a/tasks", json=_task("t-other"), headers=_bearer(other))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_revocation_survives_a_reload(app: FastAPI, client: AsyncClient) -> None:
    await _revoke(app, client, SUB, not_before=1_500_000_000)
    reloaded = await RevocationList.load(app.state.sessionmaker)
    assert reloaded.refuses({"sub": SUB, "iat": 1_400_000_000}) is not None
    assert reloaded.refuses({"sub": SUB, "iat": 1_600_000_000}) is None
    assert reloaded.refuses({"sub": "agent:other", "iat": 0}) is None


@pytest.mark.asyncio
async def test_revoking_again_only_moves_the_cutoff_forward(
    app: FastAPI, client: AsyncClient
) -> None:
    op = await _operator_token(app, client)
    first = await _revoke(app, client, SUB, op=op, not_before=1_500_000_000)
    second = await _revoke(app, client, SUB, op=op, not_before=1_400_000_000)
    assert first.json()["not_before"] == 1_500_000_000
    assert second.json()["not_before"] == 1_500_000_000


@pytest.mark.asyncio
async def test_a_colleague_cannot_revoke(app: FastAPI, client: AsyncClient) -> None:
    token = await _colleague_token(app)
    r = await client.post("/admin/token/revoke", json={"sub": "agent:x"}, headers=_bearer(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_revoke_needs_a_subject(app: FastAPI, client: AsyncClient) -> None:
    op = await _operator_token(app, client)
    r = await client.post("/admin/token/revoke", json={}, headers=_bearer(op))
    assert r.status_code == 422
