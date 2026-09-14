"""cabal#187 — the gateway audience is deployment policy, not a constant.

`LiminalConfig.jwt_aud` shipped with a knob nothing read: `admin.py` compared
presented tokens against a literal `"liminal"`, so an operator whose MCP
gateway answers to a different audience had their correctly-minted service
token rejected with `invalid_audience` and no setting that would explain it.

Two of these carry the point. `test_configured_audience_accepted` fails
against the hardcoded literal because "my-gateway" is not in
{"cabal-core", "liminal"}, and `test_default_audience_refused_once_reconfigured`
fails against a half-fix that keeps "liminal" as a permanent extra member of
`audiences`. Together they catch both shapes of a config-not-consulted
regression; measured by reverting the config read, which fails exactly these
two.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from cabal.config.identity import IdentityConfig, LiminalConfig


async def _service_token(app: FastAPI, *, aud: str) -> str:
    """A secrets-fetch service token minted for `aud`."""
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    return mint(
        signing_key=stored,
        sub="service:gateway",
        aud=aud,
        scope=["secrets-fetch"],
    )


@pytest.mark.asyncio
async def test_default_audience_accepted_when_no_identity_loaded(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """`app.state.identity` is None for an app built without a config file."""
    monkeypatch.setenv("CABAL_SECRET_archivist_files_X", "y")
    monkeypatch.setattr(app.state, "identity", None, raising=False)

    token = await _service_token(app, aud="liminal")
    r = await client.get(
        "/admin/secrets/archivist/files",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_configured_audience_accepted(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setenv("CABAL_SECRET_archivist_files_X", "y")
    monkeypatch.setattr(
        app.state,
        "identity",
        IdentityConfig(liminal=LiminalConfig(jwt_aud="my-gateway")),
        raising=False,
    )

    token = await _service_token(app, aud="my-gateway")
    r = await client.get(
        "/admin/secrets/archivist/files",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["credentials"] == {"X": "y"}


@pytest.mark.asyncio
async def test_default_audience_refused_once_reconfigured(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """Proves the setting is consulted rather than merely accepted."""
    monkeypatch.setenv("CABAL_SECRET_archivist_files_X", "y")
    monkeypatch.setattr(
        app.state,
        "identity",
        IdentityConfig(liminal=LiminalConfig(jwt_aud="my-gateway")),
        raising=False,
    )

    token = await _service_token(app, aud="liminal")
    r = await client.get(
        "/admin/secrets/archivist/files",
        headers={"Authorization": f"Bearer {token}"},
    )
    # 403, not 401: CABAL-DECISIONS D-017 (landed with cabal#124) made
    # authenticated-but-wrong-audience a 403 uniformly. A bad signature is
    # still 401; this token is validly signed and simply aimed elsewhere.
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "invalid_audience"


@pytest.mark.asyncio
async def test_operator_audience_unaffected_by_gateway_setting(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """`cabal-core` is cabal's own audience and must not move with the gateway."""
    from sqlalchemy import select

    from cabal.core.models import BootstrapCode

    monkeypatch.setenv("CABAL_SECRET_archivist_files_X", "y")
    monkeypatch.setattr(
        app.state,
        "identity",
        IdentityConfig(liminal=LiminalConfig(jwt_aud="my-gateway")),
        raising=False,
    )

    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    operator = (await client.post("/auth/bootstrap", json={"code": row.code})).json()[
        "access_token"
    ]
    r = await client.get(
        "/admin/secrets/archivist/files",
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert r.status_code == 200, r.text
