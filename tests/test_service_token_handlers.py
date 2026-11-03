"""#390: `cbl token mint-service --allowed-handler` scopes a service token to
liminal handlers, the way a persona's dispatch token already is.

The token carries `cabal_tool_extras["liminal.allowed_handlers"]`; liminal
reads that claim and refuses the rest. Without the flag the claim is left
out, and liminal lets the token call every handler.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from joserfc import jwt
from joserfc.jwk import KeySet
from sqlalchemy import select
from typer.testing import CliRunner

import cabal.cli.client as api
from cabal.cli.main import app as cli_app
from cabal.core.models import BootstrapCode


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _mint(
    app: FastAPI, client: AsyncClient, token: str | None = None, **extra
) -> httpx.Response:
    # The bootstrap code is single-use, so a test minting twice passes its token.
    token = token or await _operator_token(app, client)
    return await client.post(
        "/admin/token/mint-service",
        json={
            "sub": "service:agent-b",
            "persona": "agent-b",
            "scope": ["dispatch"],
            "aud": "liminal",
            "ttl_days": 1,
            **extra,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _claims(app: FastAPI, token: str) -> dict:
    return jwt.decode(token, app.state.jwks_provider()).claims


@pytest.mark.asyncio
async def test_allowed_handlers_land_in_the_tool_extras_claim(
    app: FastAPI, client: AsyncClient
) -> None:
    r = await _mint(app, client, allowed_handlers=["files", "web", " files "])
    assert r.status_code == 200, r.text
    assert r.json()["allowed_handlers"] == ["files", "web"]
    claims = _claims(app, r.json()["access_token"])
    assert claims["cabal_tool_extras"] == {"liminal.allowed_handlers": ["files", "web"]}


@pytest.mark.asyncio
async def test_without_allowed_handlers_the_claim_is_left_out(
    app: FastAPI, client: AsyncClient
) -> None:
    r = await _mint(app, client)
    assert r.status_code == 200, r.text
    assert r.json()["allowed_handlers"] is None
    assert "cabal_tool_extras" not in _claims(app, r.json()["access_token"])


@pytest.mark.asyncio
async def test_an_empty_list_is_kept_so_liminal_denies_every_handler(
    app: FastAPI, client: AsyncClient
) -> None:
    r = await _mint(app, client, allowed_handlers=[])
    assert r.status_code == 200, r.text
    claims = _claims(app, r.json()["access_token"])
    assert claims["cabal_tool_extras"] == {"liminal.allowed_handlers": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[""], ["  "], ["web", ""]])
async def test_a_blank_handler_name_is_refused(
    app: FastAPI, client: AsyncClient, bad: list[str]
) -> None:
    r = await _mint(app, client, allowed_handlers=bad)
    assert r.status_code == 400


def test_the_cli_flag_is_repeatable_and_omitted_when_unused(monkeypatch, tmp_path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", token_path)
    calls: list[dict] = []

    def fake_mint(**kwargs):
        calls.append(kwargs)
        return {
            "access_token": "a.b.c",
            "sub": kwargs["sub"],
            "scope": kwargs["scope"],
            "aud": kwargs["aud"],
            "expires_at": "x",
            "allowed_handlers": kwargs["allowed_handlers"],
        }

    monkeypatch.setattr(api, "mint_service_token", fake_mint)
    runner = CliRunner()
    base = ["token", "mint-service", "--persona", "agent-b", "--scope", "dispatch", "--aud", "liminal"]
    result = runner.invoke(cli_app, [*base, "--allowed-handler", "web", "--allowed-handler", "files"])
    assert result.exit_code == 0, result.output
    assert "handlers=['web', 'files']" in result.output
    result = runner.invoke(cli_app, base)
    assert result.exit_code == 0, result.output
    assert [c["allowed_handlers"] for c in calls] == [["web", "files"], None]


@pytest.mark.asyncio
async def test_liminal_lists_and_calls_only_the_tokens_handlers(
    app: FastAPI, client: AsyncClient
) -> None:
    """The issue's done-when, against a real in-process liminal: a token
    minted with `web` sees only web tools and is refused scratchpad."""
    pytest.importorskip("liminal.server")
    from liminal.handlers.scratchpad import ScratchpadHandler
    from liminal.handlers.web import WebConfig, WebHandler
    from liminal.registry import Registry
    from liminal.registry.discover import register_explicit
    from liminal.server import StaticKeySetVerifier
    from liminal.server import create_app as create_liminal_app

    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    public_jwk = dict(stored.public_jwk)
    public_jwk["kid"] = stored.kid

    registry = Registry()
    register_explicit(registry, "scratchpad", ScratchpadHandler())
    register_explicit(
        registry,
        "web",
        WebHandler(
            default_config=WebConfig(allowed_hosts=["*"]),
            http_client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok"))
            ),
            dns_resolver=lambda host: ["93.184.216.34"],
        ),
    )
    lim = create_liminal_app(
        jwt_verifier=StaticKeySetVerifier(key_set=KeySet.import_key_set({"keys": [public_jwk]})),
        registry=registry,
    )

    async def rpc(token: str, method: str, params: dict) -> dict:
        async with AsyncClient(transport=ASGITransport(app=lim), base_url="http://lim") as c:
            r = await c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={"Authorization": f"Bearer {token}"},
            )
        return r.json()

    op = await _operator_token(app, client)
    scoped = (await _mint(app, client, op, allowed_handlers=["web"])).json()["access_token"]
    listed = await rpc(scoped, "tools/list", {})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names and all(n.startswith("web.") for n in names), names
    call = {"name": "scratchpad.set", "arguments": {"key": "k", "value": "v"}}
    refused = await rpc(scoped, "tools/call", call)
    assert refused.get("error", {}).get("code") == -32002, refused  # liminal: handler not allowed

    # Control: the same mint without the flag reaches scratchpad.
    open_token = (await _mint(app, client, op)).json()["access_token"]
    listed = await rpc(open_token, "tools/list", {})
    assert "scratchpad.set" in {t["name"] for t in listed["result"]["tools"]}
    allowed = await rpc(open_token, "tools/call", call)
    assert "error" not in allowed and not allowed["result"].get("isError"), allowed
