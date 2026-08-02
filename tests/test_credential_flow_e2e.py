"""Phase OO — cross-service credential flow end-to-end.

Cabal stores a secret → limenal resolver fetches it → handler gets it.

Both services run in-process via httpx.ASGITransport — zero real network
calls, but the full HTTP path runs: cabal route → DB decrypt → JSON body
→ resolver cache → context.credentials → handler dispatch.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("limenal")

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cabal.core.models import BootstrapCode

# ---- helpers ---------------------------------------------------------------


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _mint_service_token(
    client: AsyncClient, op_token: str, *, scope: list[str]
) -> str:
    r = await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:limenal",
            "scope": scope,
            "aud": "cabal-core",
            "ttl_days": 1,
        },
        headers={"Authorization": f"Bearer {op_token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---- tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_flows_from_cabal_to_handler(
    app: FastAPI, client: AsyncClient
) -> None:
    """Full round-trip: secret stored in Cabal → fetched by limenal resolver
    → arrives in handler's ctx.credentials."""
    from limenal.protocol import Context, ToolResult, ToolSpec
    from limenal.registry import Registry
    from limenal.registry.discover import register_explicit
    from limenal.server import StaticKeySetVerifier
    from limenal.server import create_app as limenal_create_app
    from limenal.server.credentials import CabalCoreCredentialResolver

    from cabal.core.auth.jwt import mint as _mint_jwt
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    # 1. Get operator token, store a secret.
    op_token = await _operator_token(app, client)
    r = await client.post(
        "/admin/secrets/archivist/echo-creds",
        json={"key": "WEBHOOK_URL", "value": "https://hooks.example.com/test"},
        headers={"Authorization": f"Bearer {op_token}"},
    )
    assert r.status_code == 200, r.text

    # 2. Mint a secrets-fetch service token for limenal.
    svc_token = await _mint_service_token(client, op_token, scope=["secrets-fetch"])

    # 3. Wire CabalCoreCredentialResolver to in-process cabal.
    cabal_transport = ASGITransport(app=app)
    cabal_http = httpx.AsyncClient(
        transport=cabal_transport, base_url="http://cabal-test"
    )
    resolver = CabalCoreCredentialResolver(
        cabal_url="http://cabal-test",
        token_provider=lambda _t=svc_token: _t,
        http_client=cabal_http,
    )

    # 4. Mint a persona dispatch token from cabal's live signing key.
    async with open_session(app.state.sessionmaker) as session:
        stored_key = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    assert stored_key is not None
    dispatch_token = _mint_jwt(
        signing_key=stored_key,
        sub="archivist",
        aud="limenal",
        persona="archivist",
        scope=["dispatch"],
    )

    # 5. Build a limenal app with an echo handler that returns ctx.credentials.
    class _EchoCreds:
        name = "echo-creds"

        def tools(self) -> list[ToolSpec]:
            return [ToolSpec(name="dump", description="dump credentials")]

        def capability_card(self) -> dict[str, Any]:
            return {
                "handler": "echo-creds",
                "description": "test",
                "tools": [{"name": "dump", "description": "dump credentials"}],
            }

        async def dispatch(
            self, name: str, payload: dict, ctx: Context
        ) -> ToolResult:
            return ToolResult.json(
                {"creds": dict(ctx.credentials), "persona": ctx.identity}
            )

    registry = Registry()
    register_explicit(registry, "echo-creds", _EchoCreds())
    limenal_app = limenal_create_app(
        jwt_verifier=StaticKeySetVerifier(key_set=app.state.jwks_provider()),
        registry=registry,
        credential_resolver=resolver.fetch,
    )

    # 6. Call tools/call through limenal.
    limenal_transport = ASGITransport(app=limenal_app)
    async with httpx.AsyncClient(
        transport=limenal_transport, base_url="http://limenal-test"
    ) as limenal_client:
        r = await limenal_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo-creds.dump", "arguments": {}},
            },
            headers={"Authorization": f"Bearer {dispatch_token}"},
        )

    await cabal_http.aclose()

    # 7. Assert the secret from step 1 landed in the handler.
    body = r.json()
    assert body.get("error") is None, body
    result_data = json.loads(body["result"]["content"][0]["text"])
    assert result_data["creds"]["WEBHOOK_URL"] == "https://hooks.example.com/test"
    assert result_data["persona"] == "archivist"


@pytest.mark.asyncio
async def test_multiple_secrets_all_flow_through(
    app: FastAPI, client: AsyncClient
) -> None:
    """All keys stored for (persona, handler) arrive together in ctx.credentials."""
    from limenal.protocol import Context, ToolResult, ToolSpec
    from limenal.registry import Registry
    from limenal.registry.discover import register_explicit
    from limenal.server import StaticKeySetVerifier
    from limenal.server import create_app as limenal_create_app
    from limenal.server.credentials import CabalCoreCredentialResolver

    from cabal.core.auth.jwt import mint as _mint_jwt
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    op_token = await _operator_token(app, client)

    for key, value in [
        ("API_KEY", "key-abc"),
        ("API_SECRET", "secret-xyz"),
        ("REGION", "us-east-1"),
    ]:
        r = await client.post(
            "/admin/secrets/mason/echo-creds",
            json={"key": key, "value": value},
            headers={"Authorization": f"Bearer {op_token}"},
        )
        assert r.status_code == 200, r.text

    svc_token = await _mint_service_token(client, op_token, scope=["secrets-fetch"])
    cabal_transport = ASGITransport(app=app)
    cabal_http = httpx.AsyncClient(
        transport=cabal_transport, base_url="http://cabal-test"
    )
    resolver = CabalCoreCredentialResolver(
        cabal_url="http://cabal-test",
        token_provider=lambda _t=svc_token: _t,
        http_client=cabal_http,
    )

    async with open_session(app.state.sessionmaker) as session:
        stored_key = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    dispatch_token = _mint_jwt(
        signing_key=stored_key,
        sub="mason-primus",
        aud="limenal",
        persona="mason",
        scope=["dispatch"],
    )

    class _EchoCreds:
        name = "echo-creds"

        def tools(self) -> list[ToolSpec]:
            return [ToolSpec(name="dump", description="x")]

        def capability_card(self) -> dict[str, Any]:
            return {
                "handler": "echo-creds",
                "description": "x",
                "tools": [{"name": "dump", "description": "x"}],
            }

        async def dispatch(self, name, payload, ctx: Context) -> ToolResult:
            return ToolResult.json({"creds": dict(ctx.credentials)})

    registry = Registry()
    register_explicit(registry, "echo-creds", _EchoCreds())
    limenal_app = limenal_create_app(
        jwt_verifier=StaticKeySetVerifier(key_set=app.state.jwks_provider()),
        registry=registry,
        credential_resolver=resolver.fetch,
    )

    limenal_transport = ASGITransport(app=limenal_app)
    async with httpx.AsyncClient(
        transport=limenal_transport, base_url="http://limenal-test"
    ) as limenal_client:
        r = await limenal_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo-creds.dump", "arguments": {}},
            },
            headers={"Authorization": f"Bearer {dispatch_token}"},
        )

    await cabal_http.aclose()

    body = r.json()
    assert body.get("error") is None, body
    creds = json.loads(body["result"]["content"][0]["text"])["creds"]
    assert creds == {"API_KEY": "key-abc", "API_SECRET": "secret-xyz", "REGION": "us-east-1"}


@pytest.mark.asyncio
async def test_persona_isolation_different_personas_different_creds(
    app: FastAPI, client: AsyncClient
) -> None:
    """mason/notify and archivist/notify have separate credential sets; each
    persona's token only retrieves its own secrets."""
    from limenal.protocol import Context, ToolResult, ToolSpec
    from limenal.registry import Registry
    from limenal.registry.discover import register_explicit
    from limenal.server import StaticKeySetVerifier
    from limenal.server import create_app as limenal_create_app
    from limenal.server.credentials import CabalCoreCredentialResolver

    from cabal.core.auth.jwt import mint as _mint_jwt
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    op_token = await _operator_token(app, client)

    # Store distinct secrets per persona.
    await client.post(
        "/admin/secrets/mason/echo-creds",
        json={"key": "WEBHOOK_URL", "value": "https://mason.example.com/hook"},
        headers={"Authorization": f"Bearer {op_token}"},
    )
    await client.post(
        "/admin/secrets/archivist/echo-creds",
        json={"key": "WEBHOOK_URL", "value": "https://archivist.example.com/hook"},
        headers={"Authorization": f"Bearer {op_token}"},
    )

    svc_token = await _mint_service_token(client, op_token, scope=["secrets-fetch"])
    cabal_transport = ASGITransport(app=app)
    cabal_http = httpx.AsyncClient(
        transport=cabal_transport, base_url="http://cabal-test"
    )
    resolver = CabalCoreCredentialResolver(
        cabal_url="http://cabal-test",
        token_provider=lambda _t=svc_token: _t,
        http_client=cabal_http,
    )

    async with open_session(app.state.sessionmaker) as session:
        stored_key = await load_active_signing_key(
            session, master_key=app.state.master_key
        )

    class _EchoCreds:
        name = "echo-creds"

        def tools(self) -> list[ToolSpec]:
            return [ToolSpec(name="dump", description="x")]

        def capability_card(self) -> dict[str, Any]:
            return {
                "handler": "echo-creds",
                "description": "x",
                "tools": [{"name": "dump", "description": "x"}],
            }

        async def dispatch(self, name, payload, ctx: Context) -> ToolResult:
            return ToolResult.json({"creds": dict(ctx.credentials)})

    registry = Registry()
    register_explicit(registry, "echo-creds", _EchoCreds())
    limenal_app = limenal_create_app(
        jwt_verifier=StaticKeySetVerifier(key_set=app.state.jwks_provider()),
        registry=registry,
        credential_resolver=resolver.fetch,
    )

    async def _call_as(persona: str, sub: str) -> dict:
        token = _mint_jwt(
            signing_key=stored_key,
            sub=sub,
            aud="limenal",
            persona=persona,
            scope=["dispatch"],
        )
        limenal_transport = ASGITransport(app=limenal_app)
        async with httpx.AsyncClient(
            transport=limenal_transport, base_url="http://limenal-test"
        ) as lc:
            r = await lc.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "echo-creds.dump", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        body = r.json()
        assert body.get("error") is None, body
        return json.loads(body["result"]["content"][0]["text"])["creds"]

    mason_creds = await _call_as("mason", "mason-primus")
    archivist_creds = await _call_as("archivist", "archivist")

    await cabal_http.aclose()

    assert mason_creds["WEBHOOK_URL"] == "https://mason.example.com/hook"
    assert archivist_creds["WEBHOOK_URL"] == "https://archivist.example.com/hook"


@pytest.mark.asyncio
async def test_cabal_unreachable_yields_empty_creds_not_error(
    app: FastAPI, client: AsyncClient
) -> None:
    """If Cabal Core is unreachable the handler gets {} not an exception."""
    from limenal.protocol import Context, ToolResult, ToolSpec
    from limenal.registry import Registry
    from limenal.registry.discover import register_explicit
    from limenal.server import StaticKeySetVerifier
    from limenal.server import create_app as limenal_create_app
    from limenal.server.credentials import CabalCoreCredentialResolver

    from cabal.core.auth.jwt import mint as _mint_jwt
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    # Resolver that always raises a network error.
    def _dead_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cabal unreachable")

    dead_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_dead_transport),
        base_url="http://cabal-dead",
    )
    resolver = CabalCoreCredentialResolver(
        cabal_url="http://cabal-dead",
        token_provider=lambda: "tok",
        http_client=dead_http,
    )

    async with open_session(app.state.sessionmaker) as session:
        stored_key = await load_active_signing_key(
            session, master_key=app.state.master_key
        )
    dispatch_token = _mint_jwt(
        signing_key=stored_key,
        sub="archivist",
        aud="limenal",
        persona="archivist",
        scope=["dispatch"],
    )

    class _EchoCreds:
        name = "echo-creds"

        def tools(self) -> list[ToolSpec]:
            return [ToolSpec(name="dump", description="x")]

        def capability_card(self) -> dict[str, Any]:
            return {
                "handler": "echo-creds",
                "description": "x",
                "tools": [{"name": "dump", "description": "x"}],
            }

        async def dispatch(self, name, payload, ctx: Context) -> ToolResult:
            return ToolResult.json({"creds": dict(ctx.credentials)})

    registry = Registry()
    register_explicit(registry, "echo-creds", _EchoCreds())
    limenal_app = limenal_create_app(
        jwt_verifier=StaticKeySetVerifier(key_set=app.state.jwks_provider()),
        registry=registry,
        credential_resolver=resolver.fetch,
    )

    limenal_transport = ASGITransport(app=limenal_app)
    async with httpx.AsyncClient(
        transport=limenal_transport, base_url="http://limenal-test"
    ) as limenal_client:
        r = await limenal_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo-creds.dump", "arguments": {}},
            },
            headers={"Authorization": f"Bearer {dispatch_token}"},
        )

    await dead_http.aclose()

    body = r.json()
    # Must be a successful tool call result, not a JSON-RPC error.
    assert body.get("error") is None, body
    creds = json.loads(body["result"]["content"][0]["text"])["creds"]
    assert creds == {}


@pytest.mark.asyncio
async def test_resolver_cache_hit_reuses_value(
    app: FastAPI, client: AsyncClient
) -> None:
    """Two limenal calls for the same (persona, handler) only fetch from
    Cabal once (TTL has not expired)."""
    from limenal.protocol import Context, ToolResult, ToolSpec
    from limenal.registry import Registry
    from limenal.registry.discover import register_explicit
    from limenal.server import StaticKeySetVerifier
    from limenal.server import create_app as limenal_create_app
    from limenal.server.credentials import CabalCoreCredentialResolver

    from cabal.core.auth.jwt import mint as _mint_jwt
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    op_token = await _operator_token(app, client)
    await client.post(
        "/admin/secrets/archivist/echo-creds",
        json={"key": "TOKEN", "value": "tok-123"},
        headers={"Authorization": f"Bearer {op_token}"},
    )
    svc_token = await _mint_service_token(client, op_token, scope=["secrets-fetch"])

    # Wrap the cabal transport to count actual HTTP requests.
    fetch_count = {"n": 0}
    cabal_asgi = ASGITransport(app=app)

    class _CountingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            fetch_count["n"] += 1
            return await cabal_asgi.handle_async_request(request)

    cabal_http = httpx.AsyncClient(
        transport=_CountingTransport(), base_url="http://cabal-test"
    )
    resolver = CabalCoreCredentialResolver(
        cabal_url="http://cabal-test",
        token_provider=lambda _t=svc_token: _t,
        http_client=cabal_http,
    )

    async with open_session(app.state.sessionmaker) as session:
        stored_key = await load_active_signing_key(
            session, master_key=app.state.master_key
        )

    class _EchoCreds:
        name = "echo-creds"

        def tools(self) -> list[ToolSpec]:
            return [ToolSpec(name="dump", description="x")]

        def capability_card(self) -> dict[str, Any]:
            return {
                "handler": "echo-creds",
                "description": "x",
                "tools": [{"name": "dump", "description": "x"}],
            }

        async def dispatch(self, name, payload, ctx: Context) -> ToolResult:
            return ToolResult.json({"creds": dict(ctx.credentials)})

    registry = Registry()
    register_explicit(registry, "echo-creds", _EchoCreds())
    limenal_app = limenal_create_app(
        jwt_verifier=StaticKeySetVerifier(key_set=app.state.jwks_provider()),
        registry=registry,
        credential_resolver=resolver.fetch,
    )

    async def _call() -> dict:
        token = _mint_jwt(
            signing_key=stored_key,
            sub="archivist",
            aud="limenal",
            persona="archivist",
            scope=["dispatch"],
        )
        limenal_transport = ASGITransport(app=limenal_app)
        async with httpx.AsyncClient(
            transport=limenal_transport, base_url="http://limenal-test"
        ) as lc:
            r = await lc.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "echo-creds.dump", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        body = r.json()
        assert body.get("error") is None, body
        return json.loads(body["result"]["content"][0]["text"])["creds"]

    creds1 = await _call()
    creds2 = await _call()

    await cabal_http.aclose()

    assert creds1 == creds2 == {"TOKEN": "tok-123"}
    # Only 1 real Cabal HTTP request — second call was a cache hit.
    assert fetch_count["n"] == 1
