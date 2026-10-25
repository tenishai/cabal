"""Phase FF — identity.toml `tool_extras` → JWT → liminal Context.extra → handler.

Proves the full chain: an operator declares
`[personas.archivist.mcp].tool_extras = { "web.allowed_hosts" = [...] }`
in identity.toml, Cabal embeds it as the `cabal_tool_extras` JWT claim
at every dispatch token mint, liminal extracts it into Context.extra,
and liminal's web handler refuses URLs that aren't on the tightened
per-persona allowlist.
"""
from __future__ import annotations

import pytest

pytest.importorskip("liminal.server")

import json
import os
from pathlib import Path
from textwrap import dedent

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from joserfc.jwk import KeySet
from sqlalchemy import select

from cabal.core.app import create_app
from cabal.core.models import BootstrapCode


@pytest_asyncio.fixture
async def liminal_app():
    """A real liminal app with scratchpad + web handlers + an empty
    keyset to swap in cabal's signing key once it boots."""
    from liminal.handlers.scratchpad import ScratchpadHandler
    from liminal.handlers.web import WebConfig, WebHandler
    from liminal.registry import Registry
    from liminal.registry.discover import register_explicit
    from liminal.server import StaticKeySetVerifier
    from liminal.server import create_app as create_liminal_app

    # Web handler with a permissive default but no DNS surprises
    def fake_dns(host: str) -> list[str]:
        return ["93.184.216.34"]  # any public IP works

    def factory():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    headers={"content-type": "text/plain"},
                    content=b"page body",
                )
            ),
            timeout=httpx.Timeout(5.0),
        )

    web = WebHandler(
        default_config=WebConfig(allowed_hosts=["*"]),
        http_client_factory=factory,
        dns_resolver=fake_dns,
    )
    registry = Registry()
    register_explicit(registry, "scratchpad", ScratchpadHandler())
    register_explicit(registry, "web", web)

    verifier = StaticKeySetVerifier(key_set=KeySet([]))
    app = create_liminal_app(jwt_verifier=verifier, registry=registry)
    return app, verifier


@pytest_asyncio.fixture
async def cabal_app_with_persona_allowlist(tmp_path, liminal_app):
    """Cabal app with archivist's web allowlist tightened to one host."""
    lim_app, lim_verifier = liminal_app

    # Stub LLM emits a single web__fetch tool_call then a final answer.
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
                                        "name": "web__fetch",
                                        "arguments": json.dumps(
                                            {"url": "https://malicious.test/"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "saw tool result"}}]}

    identity_body = dedent(
        """
        [liminal]
        url = "http://liminal-test"

        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"

        [personas.archivist.mcp]
        enabled = true

        [personas.archivist.mcp.tool_extras]
        "web.allowed_hosts" = ["docs.python.org"]
        """
    )
    identity_path = tmp_path / "identity.toml"
    identity_path.write_text(identity_body)
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))
    fastapi_app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        # Hand cabal's public key to liminal so the JWT verifies.
        from cabal.core.auth.keys import load_active_signing_key
        from cabal.core.db import open_session

        async with open_session(fastapi_app.state.sessionmaker) as session:
            stored = await load_active_signing_key(
                session, master_key=fastapi_app.state.master_key
            )
        public_jwk = dict(stored.public_jwk)
        public_jwk["kid"] = stored.kid
        lim_verifier.key_set = KeySet.import_key_set({"keys": [public_jwk]})

        # Re-target cabal's outbound MCP at the in-process liminal app
        # and rebuild archivist's proxy with the tightened token_provider.
        lim_transport = httpx.ASGITransport(app=lim_app)
        new_mcp_client = httpx.AsyncClient(
            transport=lim_transport, base_url="http://liminal-test"
        )
        await fastapi_app.state.mcp_http_client.aclose()
        fastapi_app.state.mcp_http_client = new_mcp_client

        from cabal.core.auth.jwt import mint as mint_jwt
        from cabal.core.tools.mcp_proxy import build_proxy

        router = fastapi_app.state.dispatch_router
        archivist = router._local["archivist"]  # type: ignore[attr-defined]

        # Mirror what persona_factory does, but with our in-process client:
        # the token_provider closure must carry the tool_extras from
        # identity.toml so they land in the JWT claim.
        def token_provider():
            return mint_jwt(
                signing_key=stored,
                sub="archivist",
                aud="liminal",
                persona="archivist",
                scope=["dispatch"],
                tool_extras={"web.allowed_hosts": ["docs.python.org"]},
                ttl_seconds=300,
            )

        archivist.tool_proxy = build_proxy(
            base_url="http://liminal-test",
            persona="archivist",
            agent_name="archivist",
            token_provider=token_provider,
            http_client=new_mcp_client,
        )
        yield fastapi_app


@pytest_asyncio.fixture
async def cabal_client(cabal_app_with_persona_allowlist):
    transport = ASGITransport(app=cabal_app_with_persona_allowlist)
    async with AsyncClient(transport=transport, base_url="http://cabal-test") as c:
        yield c


@pytest.mark.asyncio
async def test_per_persona_allowlist_via_identity_toml_blocks_other_hosts(
    cabal_app_with_persona_allowlist, cabal_client
) -> None:
    """The LLM tries to fetch malicious.test; the per-persona allowlist
    only allows docs.python.org; liminal's web handler refuses; the
    final answer flows back without the agent reaching the blocked host."""

    async with cabal_app_with_persona_allowlist.state.sessionmaker() as s:
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
            "id": "t-extras",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [
                        {"kind": "text", "text": "fetch the malicious URL"}
                    ],
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
    # Final answer reached. The "saw tool result" stub fires after the
    # blocked tool call returns its allowlist-refused error.
    assert "saw tool result" in reply["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_tool_extras_claim_present_in_minted_jwt() -> None:
    """Unit: mint a JWT with tool_extras, decode it, confirm the claim
    is present."""

    from cryptography.hazmat.primitives import serialization
    from joserfc import jwt as jwt_lib
    from joserfc.jwk import KeySet, RSAKey

    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import generate_rsa_keypair

    priv, _pub = generate_rsa_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    rsa_key = RSAKey.import_key(pem)
    rsa_key.ensure_kid()
    public_jwk = dict(rsa_key.as_dict(private=False))
    public_jwk["kid"] = rsa_key.kid

    from cabal.core.auth.keys import StoredKey

    stored = StoredKey(
        kid=rsa_key.kid, private_key=priv, public_jwk=public_jwk, status="active"
    )

    token = mint(
        signing_key=stored,
        sub="archivist",
        aud="liminal",
        persona="archivist",
        scope=["dispatch"],
        tool_extras={
            "web.allowed_hosts": ["docs.python.org"],
            "files.allowed_paths": ["/Users/me/notes"],
        },
    )
    key_set = KeySet.import_key_set({"keys": [public_jwk]})
    decoded = jwt_lib.decode(token, key_set)
    claims = dict(decoded.claims)
    assert claims["cabal_tool_extras"]["web.allowed_hosts"] == ["docs.python.org"]
    assert (
        claims["cabal_tool_extras"]["files.allowed_paths"]
        == ["/Users/me/notes"]
    )


@pytest.mark.asyncio
async def test_mint_without_tool_extras_omits_claim() -> None:
    """Mint with tool_extras=None should NOT include the claim in the payload."""
    from cryptography.hazmat.primitives import serialization
    from joserfc import jwt as jwt_lib
    from joserfc.jwk import KeySet, RSAKey

    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import generate_rsa_keypair

    priv, _pub = generate_rsa_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    rsa_key = RSAKey.import_key(pem)
    rsa_key.ensure_kid()
    public_jwk = dict(rsa_key.as_dict(private=False))
    public_jwk["kid"] = rsa_key.kid
    from cabal.core.auth.keys import StoredKey

    stored = StoredKey(
        kid=rsa_key.kid, private_key=priv, public_jwk=public_jwk, status="active"
    )

    token = mint(
        signing_key=stored,
        sub="x",
        aud="liminal",
        persona="x",
        scope=["dispatch"],
    )
    key_set = KeySet.import_key_set({"keys": [public_jwk]})
    decoded = jwt_lib.decode(token, key_set)
    assert "cabal_tool_extras" not in decoded.claims
