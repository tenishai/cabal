"""#403: `POST /admin/token/mint-service` refuses to mint an operator token.

One minted there would last up to 365 days and pass `/auth/renew` without
end. Operator tokens come from bootstrap and `/auth/renew` only.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.models import BootstrapCode


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_mint_service_refuses_operator_tokens(app: FastAPI, client: AsyncClient) -> None:
    # The bootstrap code is single-use, so every request shares one token.
    op = await _operator_token(app, client)

    async def mint(sub: str, scope: list[str], aud: str):
        return await client.post(
            "/admin/token/mint-service",
            json={"sub": sub, "scope": scope, "aud": aud, "ttl_days": 365},
            headers={"Authorization": f"Bearer {op}"},
        )

    for sub, scope, aud in [
        ("operator:x", ["operator"], "cabal-core"),  # passes /auth/renew's gates
        ("service:x", ["operator"], "cabal-core"),
        ("service:x", ["dispatch", "operator"], "liminal"),
        ("operator:x", ["dispatch"], "liminal"),
        ("operator", ["dispatch"], "liminal"),
    ]:
        r = await mint(sub, scope, aud)
        assert r.status_code == 400, (sub, scope, aud, r.text)
        assert "operator tokens" in r.json()["detail"]

    # A service token is still minted, and "operator" inside a name isn't refused.
    for sub in ("service:liminal", "service:operator-tools"):
        r = await mint(sub, ["secrets-fetch"], "cabal-core")
        assert r.status_code == 200, (sub, r.text)


@pytest.mark.asyncio
async def test_service_token_ttl_is_capped_by_the_setting(app: FastAPI, client: AsyncClient) -> None:
    from types import SimpleNamespace

    from joserfc import jwt

    app.state.identity = SimpleNamespace(
        cabal=SimpleNamespace(
            operator_id="test-operator", operator_token_ttl_seconds=3600, service_token_max_ttl_days=30
        )
    )
    op = await _operator_token(app, client)
    for asked, got in ((365, 30), (7, 7)):
        r = await client.post(
            "/admin/token/mint-service",
            json={"sub": "service:x", "scope": ["dispatch"], "aud": "liminal", "ttl_days": asked},
            headers={"Authorization": f"Bearer {op}"},
        )
        assert r.status_code == 200, r.text
        claims = jwt.decode(r.json()["access_token"], app.state.jwks_provider()).claims
        assert claims["exp"] - claims["iat"] == got * 86400, (asked, got)


def test_the_service_token_cap_defaults_to_365_days_and_is_bounded() -> None:
    from pydantic import ValidationError

    from cabal.config.identity import CabalConfig

    assert CabalConfig().service_token_max_ttl_days == 365
    for bad in (0, 366):
        with pytest.raises(ValidationError):
            CabalConfig(service_token_max_ttl_days=bad)
