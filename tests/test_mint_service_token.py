"""Phase II — `POST /admin/token/mint-service` + `cbl token mint-service` CLI.

The endpoint mints a long-lived JWT operators give to a peer service
(limenal is the canonical first consumer) so it can call back into Cabal
for credential resolution.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from joserfc import jwt
from sqlalchemy import select

from cabal.core.models import BootstrapCode


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_mint_service_returns_a_verifiable_jwt(
    app: FastAPI, client: AsyncClient
) -> None:
    """The token verifies against the same JWKS Cabal serves at
    /.well-known/jwks.json."""
    token = await _operator_token(app, client)
    r = await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:limenal",
            "scope": ["secrets-fetch"],
            "aud": "cabal-core",
            "ttl_days": 30,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sub"] == "service:limenal"
    assert body["scope"] == ["secrets-fetch"]
    assert body["aud"] == "cabal-core"
    assert "access_token" in body

    # Verify against the active JWKS so the operator can be confident the
    # token is valid against the same surface limenal will check.
    key_set = app.state.jwks_provider()
    decoded = jwt.decode(body["access_token"], key_set)
    assert decoded.claims["sub"] == "service:limenal"
    assert decoded.claims["cabal_scope"] == ["secrets-fetch"]
    # 30 days from now-ish
    now = int(time.time())
    assert decoded.claims["exp"] > now + 24 * 60 * 60
    assert decoded.claims["exp"] <= now + 32 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_minted_service_token_passes_secrets_endpoint_auth(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """End-to-end: mint a service token, use it on /admin/secrets — verifies
    the secrets-fetch scope is accepted exactly as Phase GG promised."""
    monkeypatch.setenv("CABAL_SECRET_rook_test_FOO", "bar")

    op_token = await _operator_token(app, client)
    mint_r = await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:limenal",
            "scope": ["secrets-fetch"],
            "aud": "cabal-core",
            "ttl_days": 7,
        },
        headers={"Authorization": f"Bearer {op_token}"},
    )
    service_token = mint_r.json()["access_token"]

    r = await client.get(
        "/admin/secrets/rook/test",
        headers={"Authorization": f"Bearer {service_token}"},
    )
    assert r.status_code == 200
    assert r.json()["credentials"] == {"FOO": "bar"}


@pytest.mark.asyncio
async def test_mint_service_requires_operator_scope(
    app: FastAPI, client: AsyncClient
) -> None:
    """A persona/dispatch token must NOT be able to mint service tokens."""
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    persona_token = mint(
        signing_key=stored,
        sub="persona:archivist",
        aud="archivist",
        scope=["dispatch"],
    )
    r = await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:limenal",
            "scope": ["secrets-fetch"],
            "aud": "cabal-core",
            "ttl_days": 30,
        },
        headers={"Authorization": f"Bearer {persona_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_mint_service_validates_required_fields(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _operator_token(app, client)
    r = await client.post(
        "/admin/token/mint-service",
        json={"sub": "", "scope": ["x"], "aud": "y", "ttl_days": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_mint_service_caps_ttl_at_365_days(
    app: FastAPI, client: AsyncClient
) -> None:
    """Operator asks for 9999 days; we cap at 365."""
    token = await _operator_token(app, client)
    r = await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:test",
            "scope": ["secrets-fetch"],
            "aud": "cabal-core",
            "ttl_days": 9999,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    key_set = app.state.jwks_provider()
    decoded = jwt.decode(r.json()["access_token"], key_set)
    now = int(time.time())
    # ≤ 366 days (allows for clock skew + slight overage)
    assert decoded.claims["exp"] <= now + 366 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_mint_service_no_auth_rejected(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:x",
            "scope": ["y"],
            "aud": "z",
            "ttl_days": 1,
        },
    )
    assert r.status_code == 401
