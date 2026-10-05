"""Re-mint refuses a token with no `sub` instead of defaulting to "operator" (cabal#184)."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from joserfc import jwt as jwt_lib
from joserfc.jwk import RSAKey

import cabal.tui.client.inprocess as inprocess
from cabal.core.auth.jwt import DEFAULT_ISS
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.tui.client.base import BrokerError


async def _subless_token(app: FastAPI, *, scope: list[str]) -> str:
    """A validly signed token aimed at cabal-core that carries NO sub claim."""
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    claims = {
        "iss": DEFAULT_ISS,
        "aud": "cabal-core",
        "iat": 1,
        "exp": 4_000_000_000,
        "cabal_scope": scope,
    }
    header = {"alg": "RS256", "kid": stored.kid, "typ": "JWT"}
    return jwt_lib.encode(header, claims, RSAKey.import_key(stored.private_key))


_TASK = {
    "id": "t-nosub",
    "status": {
        "state": "submitted",
        "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [["operator"], ["colleague"]])
async def test_rest_remint_refuses_missing_sub(
    app: FastAPI, client: AsyncClient, scope: list[str]
) -> None:
    token = await _subless_token(app, scope=scope)
    r = await client.post(
        "/a2a/tasks",
        json={"target": "echo", "task": _TASK},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401, r.text
    assert "no sub" in r.json()["detail"]


@pytest.mark.asyncio
async def test_jsonrpc_remint_refuses_missing_sub(app: FastAPI, client: AsyncClient) -> None:
    token = await _subless_token(app, scope=["operator"])
    envelope = {
        "jsonrpc": "2.0",
        "id": "req-nosub",
        "method": "message/send",
        "params": {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
        },
    }
    r = await client.post(
        "/agents/echo", json=envelope, headers={"Authorization": f"Bearer {token}"}
    )
    body = r.json()
    assert body.get("result") is None, body
    assert "no sub" in body["error"]["message"]


def _member_audience(monkeypatch) -> None:
    """No database behind this stub app: the persona is a member, so its
    dispatch audience is its own name (cabal#234)."""

    @contextlib.asynccontextmanager
    async def no_session(_sm: Any):
        yield None

    async def own_name(_session: Any, target: str) -> str:
        return target

    monkeypatch.setattr(inprocess, "open_session", no_session)
    monkeypatch.setattr(inprocess, "dispatch_audience", own_name)


@pytest.mark.asyncio
async def test_inprocess_remint_refuses_missing_sub(monkeypatch) -> None:
    client = inprocess.InProcessBrokerClient(operator_token="t")
    client._app = SimpleNamespace(
        state=SimpleNamespace(jwks_provider=lambda: None, sessionmaker=None)
    )
    client._signing_key = object()  # type: ignore[assignment]
    _member_audience(monkeypatch)
    monkeypatch.setattr(
        inprocess, "verify", lambda *a, **k: {"aud": "cabal-core", "cabal_scope": ["operator"]}
    )
    monkeypatch.setattr(inprocess, "refusal", lambda *a, **k: None)

    def _no_mint(**_: Any) -> str:  # pragma: no cover - must not be reached
        raise AssertionError("minted a token for a subless caller")

    monkeypatch.setattr(inprocess, "mint", _no_mint)
    with pytest.raises(BrokerError, match="no sub"):
        async for _ in client.send_message("archivist", "hi"):
            pass
