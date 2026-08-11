"""Phase GG — GET /admin/secrets/{persona}/{handler}.

Source of credentials: environment variables of the form
`CABAL_SECRET_<persona>_<handler>_<KEY>`. The endpoint returns the
matching key/value pairs as a dict so liminal can hand them to its
handler at dispatch time.
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
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


# ---- happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_returns_env_vars_with_matching_prefix(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setenv("CABAL_SECRET_mason_slack_SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("CABAL_SECRET_mason_slack_SLACK_APP_TOKEN", "xapp-2")
    # Different persona — must NOT leak
    monkeypatch.setenv("CABAL_SECRET_raven_slack_SLACK_BOT_TOKEN", "xoxb-raven")
    # Different handler — must NOT leak
    monkeypatch.setenv("CABAL_SECRET_mason_github_GITHUB_TOKEN", "ghp-1")
    # Unrelated env var — must NOT leak
    monkeypatch.setenv("UNRELATED_VAR", "x")

    token = await _operator_token(app, client)
    r = await client.get(
        "/admin/secrets/mason/slack",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persona"] == "mason"
    assert body["handler"] == "slack"
    assert body["credentials"] == {
        "SLACK_BOT_TOKEN": "xoxb-1",
        "SLACK_APP_TOKEN": "xapp-2",
    }
    assert "GITHUB_TOKEN" not in body["credentials"]


@pytest.mark.asyncio
async def test_secrets_returns_empty_dict_when_no_env_set(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """A persona+handler with no env-var matches returns an empty dict,
    not 404. Handler-side decides what to do with empty credentials."""
    # Strip any pre-existing matches
    for k in list(__import__("os").environ.keys()):
        if k.startswith("CABAL_SECRET_"):
            monkeypatch.delenv(k, raising=False)
    token = await _operator_token(app, client)
    r = await client.get(
        "/admin/secrets/lonely/nothing",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["credentials"] == {}


@pytest.mark.asyncio
async def test_secrets_case_insensitive_prefix(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """Env vars are conventionally upper-snake; persona/handler in path
    can be lower. Match should still happen."""
    monkeypatch.setenv("CABAL_SECRET_ARCHIVIST_TECHNE_PRIVATE_KEY", "secret")
    token = await _operator_token(app, client)
    r = await client.get(
        "/admin/secrets/archivist/techne",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["credentials"] == {"PRIVATE_KEY": "secret"}


# ---- input safety -------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_with_special_chars_refused(
    app: FastAPI, client: AsyncClient
) -> None:
    """Names that would let an attacker shape the env-var prefix get
    refused with 400 — `persona/handler` must be [A-Za-z0-9_-]+."""
    token = await _operator_token(app, client)
    r = await client.get(
        "/admin/secrets/mason$evil/slack",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_persona_with_path_traversal_refused(
    app: FastAPI, client: AsyncClient
) -> None:
    """`..` would route to a different URL anyway, but verify the regex
    refuses it explicitly when FastAPI passes it through."""
    token = await _operator_token(app, client)
    r = await client.get(
        "/admin/secrets/mason/slack..",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


# ---- auth gate ---------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_requires_auth_scope(
    app: FastAPI, client: AsyncClient
) -> None:
    """A persona-scoped (dispatch-only) token is refused."""
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
        aud="cabal-core",
        scope=["dispatch"],
    )
    r = await client.get(
        "/admin/secrets/mason/slack",
        headers={"Authorization": f"Bearer {persona_token}"},
    )
    assert r.status_code == 403
    assert "operator" in r.json()["detail"] or "secrets-fetch" in r.json()["detail"]


@pytest.mark.asyncio
async def test_secrets_accepts_secrets_fetch_scope(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    """A token with `secrets-fetch` scope (the liminal-service token) is
    accepted even without operator scope."""
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    monkeypatch.setenv("CABAL_SECRET_archivist_files_X", "y")

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    service_token = mint(
        signing_key=stored,
        sub="service:liminal",
        aud="liminal",
        scope=["secrets-fetch"],
    )
    r = await client.get(
        "/admin/secrets/archivist/files",
        headers={"Authorization": f"Bearer {service_token}"},
    )
    assert r.status_code == 200
    assert r.json()["credentials"] == {"X": "y"}


@pytest.mark.asyncio
async def test_secrets_no_auth_rejected(client: AsyncClient) -> None:
    r = await client.get("/admin/secrets/x/y")
    assert r.status_code == 401
