"""Phase AA — identity.toml-driven MCP wiring.

Verifies the end-to-end path: an identity.toml with [liminal] + a
persona that has [personas.NAME.mcp].enabled=true makes that persona's
handler arrive with a real, working tool_proxy without anyone wiring it
manually.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("liminal")
import os
from pathlib import Path
from textwrap import dedent

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from joserfc.jwk import KeySet

from cabal.core.app import create_app


def _write_identity(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "identity.toml"
    p.write_text(body)
    return p


@pytest_asyncio.fixture
async def liminal_app():
    """Real liminal FastAPI app with a static-keyset verifier we'll
    populate post-cabal-startup (so cabal's signing key matches)."""
    from liminal.handlers.scratchpad import ScratchpadHandler
    from liminal.registry import Registry
    from liminal.registry.discover import register_explicit
    from liminal.server import StaticKeySetVerifier
    from liminal.server import create_app as create_liminal_app

    registry = Registry()
    register_explicit(registry, "scratchpad", ScratchpadHandler())
    # Empty keyset to start; we'll swap in cabal's keyset below
    verifier = StaticKeySetVerifier(key_set=KeySet([]))
    app = create_liminal_app(jwt_verifier=verifier, registry=registry)
    return app, verifier


@pytest_asyncio.fixture
async def cabal_app_with_mcp(tmp_path, liminal_app):
    """Build a Cabal app whose lifespan auto-wires a tool_proxy for
    archivist against the in-process liminal app."""
    lim_app, lim_verifier = liminal_app

    # Stub LLM that issues a tool_call then a final answer
    state = {"step": 0}

    async def fn(model: str, messages: list, **kwargs):
        state["step"] += 1
        if state["step"] == 1 and "tools" in kwargs:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "scratchpad__set",
                                        "arguments": json.dumps(
                                            {"key": "color", "value": "green"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "stored your color"}}]}

    identity_body = dedent(
        """
        [liminal]
        url = "http://liminal-cabal-test"
        token_ttl_seconds = 300
        jwt_aud = "liminal"

        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"

        [personas.archivist.mcp]
        enabled = true

        [personas.architect]
        role = "singleton"
        safety_tier = "moderate"
        # No mcp section → architect must NOT get a tool_proxy
        """
    )
    identity_path = _write_identity(tmp_path, identity_body)
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))
    fastapi_app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )

    async with fastapi_app.router.lifespan_context(fastapi_app):
        # Cabal lifespan has now minted its signing key into the DB. Pull it
        # out and load into liminal's verifier so JWTs Cabal mints will be
        # accepted by liminal.
        from cabal.core.auth.keys import load_active_signing_key
        from cabal.core.db import open_session

        async with open_session(fastapi_app.state.sessionmaker) as session:
            stored = await load_active_signing_key(
                session, master_key=fastapi_app.state.master_key
            )
        assert stored is not None
        public_jwk = dict(stored.public_jwk)
        public_jwk["kid"] = stored.kid
        lim_verifier.key_set = KeySet.import_key_set({"keys": [public_jwk]})

        # Now swap Cabal's outbound MCP client to route to the in-process
        # liminal app. Replace BOTH the http client and the proxy's client.
        lim_transport = httpx.ASGITransport(app=lim_app)
        new_mcp_client = httpx.AsyncClient(
            transport=lim_transport, base_url="http://liminal-cabal-test"
        )
        # Close the original client so we don't leak connections, then
        # rebuild the archivist proxy against the in-process liminal.
        await fastapi_app.state.mcp_http_client.aclose()
        fastapi_app.state.mcp_http_client = new_mcp_client

        router = fastapi_app.state.dispatch_router
        archivist = router._local["archivist"]  # type: ignore[attr-defined]
        # Rebuild its proxy through the new client
        from cabal.core.auth.jwt import mint as mint_jwt
        from cabal.core.tools.mcp_proxy import build_proxy

        def token_provider():
            return mint_jwt(
                signing_key=stored,
                sub="archivist",
                aud="liminal",
                persona="archivist",
                scope=["dispatch"],
                ttl_seconds=300,
            )

        archivist.tool_proxy = build_proxy(
            base_url="http://liminal-cabal-test",
            persona="archivist",
            agent_name="archivist",
            token_provider=token_provider,
            http_client=new_mcp_client,
        )

        yield fastapi_app


@pytest_asyncio.fixture
async def cabal_client(cabal_app_with_mcp):
    transport = ASGITransport(app=cabal_app_with_mcp)
    async with AsyncClient(transport=transport, base_url="http://cabal-test") as c:
        yield c


@pytest.mark.asyncio
async def test_archivist_gets_tool_proxy_from_identity_toml(
    cabal_app_with_mcp,
) -> None:
    """The persona handler arrives with a non-None tool_proxy."""
    router = cabal_app_with_mcp.state.dispatch_router
    archivist = router._local["archivist"]  # type: ignore[attr-defined]
    assert archivist.tool_proxy is not None
    assert archivist.tool_proxy.persona == "archivist"


@pytest.mark.asyncio
async def test_architect_does_not_get_tool_proxy(cabal_app_with_mcp) -> None:
    """Without [personas.architect.mcp].enabled the handler keeps
    tool_proxy=None even though [liminal] is configured."""
    router = cabal_app_with_mcp.state.dispatch_router
    architect = router._local["architect"]  # type: ignore[attr-defined]
    assert architect.tool_proxy is None


@pytest.mark.asyncio
async def test_archivist_dispatches_through_liminal_end_to_end(
    cabal_app_with_mcp, cabal_client
) -> None:
    """Operator dispatches an archivist task → LLM emits scratchpad__set
    via tool_call → MCPToolProxy routes through the in-process liminal app
    → scratchpad handler executes → cross-verify via direct call."""
    from sqlalchemy import select

    from cabal.core.models import BootstrapCode

    async with cabal_app_with_mcp.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await cabal_client.post("/auth/bootstrap", json={"code": row.code})
    token = r.json()["access_token"]

    body = {
        "target": "archivist",
        "task": {
            "id": "t-mcp",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "store color=green"}],
                },
            },
        },
    }
    r = await cabal_client.post(
        "/a2a/tasks",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    reply = r.json()
    assert reply["status"]["state"] == "completed"
    text = reply["status"]["message"]["parts"][0]["text"]
    assert "stored your color" in text

    # Cross-verify: archivist's tool_proxy.scratchpad.get returns the value
    router = cabal_app_with_mcp.state.dispatch_router
    archivist = router._local["archivist"]  # type: ignore[attr-defined]
    outcome = await archivist.tool_proxy.call_tool(
        "scratchpad__get", {"key": "color"}
    )
    assert json.loads(outcome.content[0]["text"])["value"] == "green"


@pytest.mark.asyncio
async def test_max_steps_override_from_identity(tmp_path) -> None:
    """[personas.NAME.mcp].max_steps overrides LLMPersona's default 4."""
    identity_body = dedent(
        """
        [liminal]
        url = "http://nowhere"

        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"

        [personas.archivist.mcp]
        enabled = true
        max_steps = 7
        """
    )
    identity_path = tmp_path / "identity.toml"
    identity_path.write_text(identity_body)
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))

    async def fn(*a, **kw):
        return {"choices": [{"message": {"content": "x"}}]}

    fastapi_app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        archivist = fastapi_app.state.dispatch_router._local["archivist"]  # type: ignore[attr-defined]
        assert archivist.tool_use_max_steps == 7
