"""End-to-end: identity.toml-declared persona dispatches through the daemon."""

from __future__ import annotations

from pathlib import Path

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cabal.core.app import create_app
from cabal.core.models import BootstrapCode


def _write_identity(tmp_path: Path) -> Path:
    p = tmp_path / "identity.toml"
    p.write_text(
        """
[cabal]
port = 7400

[litellm]
api_base = "http://litellm:4000"
api_key_env = "DOES_NOT_EXIST"

[personas.archivist]
description = "Memory and search"
role = "singleton"
safety_tier = "moderate"

[personas.ghost]
description = "No implementation"
role = "singleton"
safety_tier = "strict"
"""
    )
    return p


@pytest_asyncio.fixture
async def app_with_personas(tmp_path, monkeypatch):
    # conftest's autouse fixture already chdirs to tmp_path, creates data/,
    # sets CABAL_MASTER_KEY + CABAL_DB_URL. We just write identity.toml here.
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))
    identity_path = _write_identity(tmp_path)

    async def stub_acompletion(model: str, messages: list, **kwargs):
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"[{model}] remembered: {last_user}",
                    }
                }
            ],
            "model": model,
        }

    fastapi_app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=stub_acompletion,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest_asyncio.fixture
async def client(app_with_personas):
    transport = ASGITransport(app=app_with_personas)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _bootstrap_token(app, client) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            (
                await s.execute(
                    select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
                )
            )
            .scalar_one()
        )
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_archivist_registered_and_dispatchable(
    app_with_personas, client
) -> None:
    token = await _bootstrap_token(app_with_personas, client)
    # /admin/agents should show archivist registered with a handler
    r = await client.get(
        "/admin/agents", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    names = {a["name"] for a in r.json()["agents"]}
    assert "archivist" in names
    assert "ghost" in names  # bulletin row exists for unknown persona

    # Dispatch hits the stubbed acompletion
    body = {
        "target": "archivist",
        "task": {
            "id": "t-1",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "what is on my mind?"}],
                },
            },
        },
    }
    r = await client.post(
        "/a2a/tasks", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    reply = r.json()
    assert reply["status"]["state"] == "completed"
    text = reply["status"]["message"]["parts"][0]["text"]
    assert "remembered: what is on my mind?" in text
    # Model was used from the persona's default
    assert "anthropic" in text


@pytest.mark.asyncio
async def test_dispatch_to_persona_without_handler_returns_clean_error(
    app_with_personas, client
) -> None:
    """`ghost` is declared in identity.toml but has no PERSONA_REGISTRY entry.
    Dispatch should return 502 with a clear "no handler" message, not a
    transport error."""
    token = await _bootstrap_token(app_with_personas, client)
    body = {
        "target": "ghost",
        "task": {
            "id": "t-2",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hi"}],
                },
            },
        },
    }
    r = await client.post(
        "/a2a/tasks", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 502
    assert "no handler" in r.json()["detail"]
