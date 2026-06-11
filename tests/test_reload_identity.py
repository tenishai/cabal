"""Phase Y: POST /admin/reload/identity rebuilds persona handlers in place."""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cabal.core.app import create_app
from cabal.core.models import BootstrapCode


async def _bootstrap_token(app, client) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


@pytest_asyncio.fixture
async def reload_app_factory(tmp_path):
    """Build an app whose identity.toml we can rewrite mid-test."""
    identity_path = tmp_path / "identity.toml"
    identity_path.write_text(
        dedent(
            """
            [personas.archivist]
            role = "singleton"
            safety_tier = "moderate"
            """
        )
    )

    async def fn(model: str, messages: list, **kwargs):
        return {"choices": [{"message": {"content": "stub"}}]}

    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))
    fastapi_app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )
    yield fastapi_app, identity_path


@pytest_asyncio.fixture
async def reload_app(reload_app_factory):
    fastapi_app, _ = reload_app_factory
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest_asyncio.fixture
async def reload_client(reload_app):
    transport = ASGITransport(app=reload_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_reload_picks_up_new_persona(
    reload_app, reload_client, reload_app_factory
) -> None:
    _, identity_path = reload_app_factory
    token = await _bootstrap_token(reload_app, reload_client)
    router = reload_app.state.dispatch_router
    assert router.has_local("archivist")
    assert not router.has_local("architect")

    identity_path.write_text(
        dedent(
            """
            [personas.archivist]
            role = "singleton"
            safety_tier = "moderate"

            [personas.architect]
            role = "singleton"
            safety_tier = "moderate"
            """
        )
    )

    r = await reload_client.post(
        "/admin/reload/identity", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reloaded"] is True
    assert "architect" in body["registered"]
    assert router.has_local("architect")
    assert router.has_local("archivist")


@pytest.mark.asyncio
async def test_reload_applies_system_prompt_override(
    reload_app, reload_client, reload_app_factory
) -> None:
    _, identity_path = reload_app_factory
    token = await _bootstrap_token(reload_app, reload_client)
    router = reload_app.state.dispatch_router
    before_prompt = router._local["archivist"].system_prompt  # type: ignore[attr-defined]

    identity_path.write_text(
        dedent(
            """
            [personas.archivist]
            role = "singleton"
            safety_tier = "moderate"
            system_prompt_override = "BRAND NEW PROMPT after reload."
            """
        )
    )
    r = await reload_client.post(
        "/admin/reload/identity", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text

    after_prompt = router._local["archivist"].system_prompt  # type: ignore[attr-defined]
    assert after_prompt != before_prompt
    assert "BRAND NEW PROMPT after reload." in after_prompt


@pytest.mark.asyncio
async def test_reload_preserves_litellm_client_instance(
    reload_app, reload_client, reload_app_factory
) -> None:
    """Breaker + budget state should survive reload — same LiteLLMClient."""
    _, _ = reload_app_factory
    token = await _bootstrap_token(reload_app, reload_client)
    client_before = reload_app.state.litellm_client
    r = await reload_client.post(
        "/admin/reload/identity", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    assert reload_app.state.litellm_client is client_before


@pytest.mark.asyncio
async def test_reload_404_when_identity_missing(reload_app, reload_client) -> None:
    token = await _bootstrap_token(reload_app, reload_client)
    reload_app.state.identity_path.unlink()
    r = await reload_client.post(
        "/admin/reload/identity", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reload_requires_operator_scope(reload_app, reload_client) -> None:
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(reload_app.state.sessionmaker) as session:
        stored = await load_active_signing_key(
            session, master_key=reload_app.state.master_key
        )
    persona_token = mint(
        signing_key=stored, sub="persona:x", aud="cabal-core", scope=["dispatch"]
    )
    r = await reload_client.post(
        "/admin/reload/identity",
        headers={"Authorization": f"Bearer {persona_token}"},
    )
    assert r.status_code == 403
