"""POST /auth/liminal-token: the colleague -> gateway token exchange (C1).

Standing identity (the year-scale colleague JWT, aud cabal-core) must not
open the MCP gateway; this route swaps it for a fifteen-minute
aud="liminal" token whose persona is the agent name. The tests pin the
swap's walls: colleague scope only, identity carried through verbatim,
short expiry, and nothing granted by the exchange itself.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from joserfc import jwt as jwt_lib
from joserfc.jwk import RSAKey

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session


async def _signing_key(app: FastAPI):
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return stored


def _decode(app_key, token: str) -> dict:
    public = RSAKey.import_key(app_key.private_key)
    return jwt_lib.decode(token, public, algorithms=["RS256"]).claims


@pytest.mark.asyncio
async def test_colleague_token_exchanges_for_a_liminal_token(
    app: FastAPI, client: AsyncClient
) -> None:
    stored = await _signing_key(app)
    colleague = mint(
        signing_key=stored, sub="agent:ledger", aud="cabal-core", scope=["colleague"]
    )
    resp = await client.post(
        "/auth/liminal-token", headers={"Authorization": f"Bearer {colleague}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["aud"] == "liminal"
    assert body["persona"] == "ledger"

    claims = _decode(stored, body["access_token"])
    assert claims["aud"] == "liminal"
    assert claims["sub"] == "agent:ledger"
    assert claims["cabal_persona"] == "ledger"
    assert claims["cabal_scope"] == ["mcp"]
    assert claims["exp"] - claims["iat"] == 15 * 60


@pytest.mark.asyncio
async def test_dispatch_scope_cannot_exchange(app: FastAPI, client: AsyncClient) -> None:
    stored = await _signing_key(app)
    dispatch = mint(
        signing_key=stored, sub="persona:archivist", aud="cabal-core", scope=["dispatch"]
    )
    resp = await client.post(
        "/auth/liminal-token", headers={"Authorization": f"Bearer {dispatch}"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_operator_scope_cannot_exchange(app: FastAPI, client: AsyncClient) -> None:
    stored = await _signing_key(app)
    operator = mint(
        signing_key=stored, sub="operator:kyle", aud="cabal-core", scope=["operator"]
    )
    resp = await client.post(
        "/auth/liminal-token", headers={"Authorization": f"Bearer {operator}"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_exchange_rejected(client: AsyncClient) -> None:
    resp = await client.post("/auth/liminal-token")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_persona_shaped_sub_falls_back_to_persona_claim(
    app: FastAPI, client: AsyncClient
) -> None:
    stored = await _signing_key(app)
    token = mint(
        signing_key=stored,
        sub="persona:wren",
        aud="cabal-core",
        persona="wren",
        scope=["colleague"],
    )
    resp = await client.post(
        "/auth/liminal-token", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["persona"] == "wren"


@pytest.mark.asyncio
async def test_wrong_audience_is_refused_with_403(
    app: FastAPI, client: AsyncClient
) -> None:
    """The audience gate, isolated (#191).

    The token carries colleague scope and an agent identity, so it passes
    every other check and fails ONLY on audience: delete the audience gate
    and it buys a gateway token (200). 403 rather than 401 per
    CABAL-DECISIONS D-017: the signature is valid, the realm is wrong.
    """
    stored = await _signing_key(app)
    aimed_elsewhere = mint(
        signing_key=stored, sub="agent:ledger", aud="liminal", scope=["colleague"]
    )
    resp = await client.post(
        "/auth/liminal-token", headers={"Authorization": f"Bearer {aimed_elsewhere}"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "invalid_audience"


@pytest.mark.asyncio
async def test_exchanged_token_cannot_be_re_exchanged(
    app: FastAPI, client: AsyncClient
) -> None:
    """The 15 minutes is a wall, not a refresh interval.

    An exchanged token fails TWO gates: it wears `aud="liminal"` and carries
    `scope=["mcp"]`, not colleague. So this test cannot pin either gate on
    its own; with the audience gate deleted it still gets 403 from the scope
    gate (measured in #191). `test_wrong_audience_is_refused_with_403` is the
    one that pins audience. This one pins the end-to-end property.
    """
    stored = await _signing_key(app)
    colleague = mint(
        signing_key=stored, sub="agent:ledger", aud="cabal-core", scope=["colleague"]
    )
    first = await client.post(
        "/auth/liminal-token", headers={"Authorization": f"Bearer {colleague}"}
    )
    assert first.status_code == 200, first.text
    exchanged = first.json()["access_token"]

    again = await client.post(
        "/auth/liminal-token", headers={"Authorization": f"Bearer {exchanged}"}
    )
    assert again.status_code == 403, again.text
