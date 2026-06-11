"""POST /admin/secrets/{persona}/{handler}, DELETE, GET /admin/secrets list."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
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


# ---- set + fetch roundtrip ---------------------------------------------


@pytest.mark.asyncio
async def test_set_then_fetch_returns_value(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)

    r = await client.post(
        "/admin/secrets/rook/slack",
        json={"key": "SLACK_BOT_TOKEN", "value": "xoxb-stored"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True

    r2 = await client.get(
        "/admin/secrets/rook/slack",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r2.json()
    assert body["credentials"] == {"SLACK_BOT_TOKEN": "xoxb-stored"}
    assert body["sources"]["db"] == 1
    assert body["sources"]["env"] == 0


@pytest.mark.asyncio
async def test_env_overrides_db(app: FastAPI, client: AsyncClient, monkeypatch) -> None:
    """Same KEY in env wins over DB."""
    token = await _operator_token(app, client)
    await client.post(
        "/admin/secrets/rook/slack",
        json={"key": "SLACK_BOT_TOKEN", "value": "from-db"},
        headers={"Authorization": f"Bearer {token}"},
    )
    monkeypatch.setenv("CABAL_SECRET_rook_slack_SLACK_BOT_TOKEN", "from-env")

    r = await client.get(
        "/admin/secrets/rook/slack",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    assert body["credentials"]["SLACK_BOT_TOKEN"] == "from-env"


@pytest.mark.asyncio
async def test_set_rejects_bad_key_format(app: FastAPI, client: AsyncClient) -> None:
    """KEY must look like an env-var (upper-snake) for consistency with
    capability_card.credential_keys."""
    token = await _operator_token(app, client)
    for bad in ("lowercase_key", "Mixed-Case", "1STARTS_WITH_DIGIT", "spaces in key"):
        r = await client.post(
            "/admin/secrets/rook/slack",
            json={"key": bad, "value": "x"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400, f"expected 400 for {bad!r}, got {r.status_code}"


@pytest.mark.asyncio
async def test_set_rejects_empty_value(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    r = await client.post(
        "/admin/secrets/rook/slack",
        json={"key": "TOKEN", "value": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


# ---- unset ----------------------------------------------------------


@pytest.mark.asyncio
async def test_unset_deletes_secret(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    await client.post(
        "/admin/secrets/rook/slack",
        json={"key": "K", "value": "v"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = await client.delete(
        "/admin/secrets/rook/slack/K",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.json()["deleted"] is True

    r2 = await client.get(
        "/admin/secrets/rook/slack",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.json()["credentials"] == {}


@pytest.mark.asyncio
async def test_unset_missing_returns_deleted_false(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _operator_token(app, client)
    r = await client.delete(
        "/admin/secrets/rook/slack/NEVER_EXISTED",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["deleted"] is False


# ---- list -------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_names_not_values(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _operator_token(app, client)
    await client.post(
        "/admin/secrets/rook/slack",
        json={"key": "TOKEN", "value": "secret"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = await client.get(
        "/admin/secrets", headers={"Authorization": f"Bearer {token}"}
    )
    body = r.json()
    assert len(body["entries"]) == 1
    e = body["entries"][0]
    assert e == {"persona": "rook", "handler": "slack", "key": "TOKEN"}
    # No value in response — the contract is names-only
    assert "value" not in e
    assert "secret" not in r.text


@pytest.mark.asyncio
async def test_list_filters_work(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    for p in ("rook", "raven"):
        for h in ("slack", "github"):
            await client.post(
                f"/admin/secrets/{p}/{h}",
                json={"key": "K", "value": "v"},
                headers={"Authorization": f"Bearer {token}"},
            )

    r = await client.get(
        "/admin/secrets",
        params={"persona": "rook"},
        headers={"Authorization": f"Bearer {token}"},
    )
    rook_only = r.json()["entries"]
    assert {e["persona"] for e in rook_only} == {"rook"}
    assert {e["handler"] for e in rook_only} == {"slack", "github"}

    r = await client.get(
        "/admin/secrets",
        params={"persona": "rook", "handler": "slack"},
        headers={"Authorization": f"Bearer {token}"},
    )
    pair = r.json()["entries"]
    assert pair == [{"persona": "rook", "handler": "slack", "key": "K"}]


# ---- auth gates -------------------------------------------------------


@pytest.mark.asyncio
async def test_set_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    persona_token = mint(
        signing_key=stored, sub="x", aud="x", scope=["dispatch"]
    )
    r = await client.post(
        "/admin/secrets/rook/slack",
        json={"key": "K", "value": "v"},
        headers={"Authorization": f"Bearer {persona_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    persona_token = mint(
        signing_key=stored, sub="x", aud="x", scope=["dispatch"]
    )
    r = await client.get(
        "/admin/secrets",
        headers={"Authorization": f"Bearer {persona_token}"},
    )
    assert r.status_code == 403
