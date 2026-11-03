"""#395: an operator token can be renewed before it expires.

`POST /auth/renew` swaps a valid operator token for a fresh one, and `cbl`
renews a saved operator token by itself when it has under ten minutes left,
so an operator session no longer ends an hour after `cbl login`.
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from joserfc import jwt
from sqlalchemy import select
from typer.testing import CliRunner

import cabal.cli.client as api
from cabal.cli.main import app as cli_app
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode

OPERATOR = "operator:test-operator"  # conftest sets CABAL_OPERATOR_ID=test-operator


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _signed(app: FastAPI, **claims) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    return mint(signing_key=stored, **claims)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _claims(app: FastAPI, token: str) -> dict:
    return jwt.decode(token, app.state.jwks_provider()).claims


# --- the route ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_operator_token_renews_to_a_later_one(
    app: FastAPI, client: AsyncClient
) -> None:
    old = await _signed(app, sub=OPERATOR, aud="cabal-core", scope=["operator"],
                        ttl_seconds=3600, now=int(time.time()) - 3000)
    r = await client.post("/auth/renew", headers=_bearer(old))
    assert r.status_code == 200, r.text
    new = _claims(app, r.json()["access_token"])
    assert (new["sub"], new["aud"], new["cabal_scope"]) == (OPERATOR, "cabal-core", ["operator"])
    assert new["exp"] > _claims(app, old)["exp"]
    # The renewed token works where an operator token is needed.
    assert (await client.get("/admin/agents", headers=_bearer(r.json()["access_token"]))).status_code == 200


@pytest.mark.asyncio
async def test_an_expired_token_does_not_renew(app: FastAPI, client: AsyncClient) -> None:
    lapsed = await _signed(app, sub=OPERATOR, aud="cabal-core", scope=["operator"],
                           ttl_seconds=60, now=int(time.time()) - 3600)
    r = await client.post("/auth/renew", headers=_bearer(lapsed))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_operator_token_does_not_renew(app: FastAPI, client: AsyncClient) -> None:
    """A renewal minted after the cutoff would pass every later check, so
    the route itself must refuse a revoked token."""
    old = await _signed(app, sub=OPERATOR, aud="cabal-core", scope=["operator"],
                        now=int(time.time()) - 120)
    revoked = await client.post("/admin/token/revoke", json={"sub": OPERATOR}, headers=_bearer(old))
    assert revoked.status_code == 200, revoked.text
    r = await client.post("/auth/renew", headers=_bearer(old))
    assert r.status_code == 401
    assert "revoked" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [
    {"sub": "persona:archivist", "aud": "cabal-core", "scope": ["dispatch"]},
    {"sub": "agent:room", "aud": "cabal-core", "scope": ["colleague"]},
    {"sub": "service:x", "aud": "cabal-core", "scope": ["operator"]},  # operator scope, wrong subject
    {"sub": OPERATOR, "aud": "liminal", "scope": ["operator"]},
])
async def test_only_an_operator_token_for_cabal_core_renews(
    app: FastAPI, client: AsyncClient, claims: dict
) -> None:
    r = await client.post("/auth/renew", headers=_bearer(await _signed(app, **claims)))
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_renew_needs_a_bearer(client: AsyncClient) -> None:
    assert (await client.post("/auth/renew")).status_code == 401


@pytest.mark.asyncio
async def test_the_operator_ttl_setting_applies_to_login_and_renewal(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.identity = SimpleNamespace(
        cabal=SimpleNamespace(operator_id="test-operator", operator_token_ttl_seconds=8 * 3600)
    )
    token = await _operator_token(app, client)
    assert _claims(app, token)["exp"] - _claims(app, token)["iat"] == 8 * 3600
    renewed = (await client.post("/auth/renew", headers=_bearer(token))).json()["access_token"]
    assert _claims(app, renewed)["exp"] - _claims(app, renewed)["iat"] == 8 * 3600


def test_the_operator_ttl_defaults_to_an_hour_and_has_a_floor() -> None:
    from pydantic import ValidationError

    from cabal.config.identity import CabalConfig

    assert CabalConfig().operator_token_ttl_seconds == 3600
    with pytest.raises(ValidationError):
        CabalConfig(operator_token_ttl_seconds=10)


# --- the CLI -----------------------------------------------------------------


def _unsigned(**claims) -> str:
    """A token-shaped string whose payload the CLI can read; the CLI never
    verifies, the daemon does."""
    def part(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


@pytest.fixture
def store(monkeypatch, tmp_path):
    path = tmp_path / "token"
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", path)
    return api.TokenStore()


def _fake_renew(monkeypatch, result):
    calls: list[str] = []

    def fake(*, token: str):
        calls.append(token)
        if isinstance(result, Exception):
            raise result
        return {"access_token": result}

    monkeypatch.setattr(api, "renew_token", fake)
    return calls


@pytest.mark.parametrize("left, renews", [(300, True), (3000, False), (-5, False)])
def test_load_renews_an_operator_token_only_near_its_expiry(monkeypatch, store, left, renews) -> None:
    old = _unsigned(sub=OPERATOR, cabal_scope=["operator"], exp=int(time.time()) + left)
    store.save(old)
    calls = _fake_renew(monkeypatch, "new-token")
    got = store.load()
    assert (got, calls) == (("new-token", [old]) if renews else (old, []))
    assert store.path.read_text() == got


def test_load_leaves_other_tokens_alone(monkeypatch, store) -> None:
    calls = _fake_renew(monkeypatch, "new-token")
    for token in (_unsigned(sub="persona:x", cabal_scope=["dispatch"], exp=int(time.time()) + 60),
                  "not-a-jwt"):
        store.save(token)
        assert store.load() == token
    assert calls == []


def test_a_failed_renewal_returns_the_old_token(monkeypatch, store) -> None:
    old = _unsigned(sub=OPERATOR, cabal_scope=["operator"], exp=int(time.time()) + 60)
    store.save(old)
    _fake_renew(monkeypatch, httpx.ConnectError("refused"))
    assert store.load() == old
    assert store.path.read_text() == old


def test_load_without_renew_never_calls_the_daemon(monkeypatch, store) -> None:
    old = _unsigned(sub=OPERATOR, cabal_scope=["operator"], exp=int(time.time()) + 60)
    store.save(old)
    assert _fake_renew(monkeypatch, "new") == [] and store.load(renew=False) == old


def test_the_token_file_is_owner_only_and_replaced_whole(store) -> None:
    store.save("one")
    store.save("two")
    assert store.path.read_text() == "two"
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert not [p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")]


def test_cbl_token_renew_saves_the_new_token(monkeypatch, store) -> None:
    store.save("old")
    calls = _fake_renew(monkeypatch, "fresh")
    result = CliRunner().invoke(cli_app, ["token", "renew"])
    assert result.exit_code == 0, result.output
    assert calls == ["old"] and store.path.read_text() == "fresh"


def test_cbl_token_renew_reports_a_refusal(monkeypatch, store) -> None:
    store.save("old")
    resp = httpx.Response(401, json={"detail": "token expired"},
                          request=httpx.Request("POST", "http://x/auth/renew"))
    _fake_renew(monkeypatch, httpx.HTTPStatusError("401", request=resp.request, response=resp))
    result = CliRunner().invoke(cli_app, ["token", "renew"])
    assert result.exit_code == 1
    assert "renew failed: token expired" in result.output
    assert store.path.read_text() == "old"


def test_cbl_task_wait_picks_up_a_renewed_token(monkeypatch, store) -> None:
    """The poller used the token it started with, so it died with the
    token an hour in. It now reloads the token on every poll."""
    store.save("t0")
    seen: list[str] = []
    states = iter(["working", "working", "completed"])

    def fake_get_task(*, token, target, task_id):
        seen.append(token)
        store.save(f"t{len(seen)}")  # as a renewal by another process would
        return {"status": {"state": next(states), "message": {"parts": []}}}

    monkeypatch.setattr(api, "get_task", fake_get_task)
    result = CliRunner().invoke(
        cli_app, ["task", "abc", "--wait", "--interval", "0.05", "--target", "echo"]
    )
    assert result.exit_code == 0, result.output
    assert seen == ["t0", "t1", "t2"]
