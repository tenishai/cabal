"""InProcessBrokerClient keeps a guardrail refusal distinct (cabal#157).

No app boot: the client's app, router and token check are stubbed, so this
runs without the techne/liminal siblings the full in-process tests need.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import cabal.tui.client.inprocess as inprocess
from cabal.core.broker.router import DispatchError, GuardrailBlocked
from cabal.tui.client.base import BrokerError


class _RaisingRouter:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def dispatch_streaming(self, **_: Any) -> AsyncIterator[str]:
        raise self._exc
        yield ""  # pragma: no cover - makes this an async generator


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


def _client(monkeypatch, exc: Exception) -> inprocess.InProcessBrokerClient:
    client = inprocess.InProcessBrokerClient(operator_token="t")
    client._app = SimpleNamespace(
        state=SimpleNamespace(jwks_provider=lambda: None, sessionmaker=None)
    )
    client._router = _RaisingRouter(exc)
    _member_audience(monkeypatch)
    # aud == persona: an already-scoped token, so no minting is needed.
    monkeypatch.setattr(inprocess, "verify", lambda *a, **k: {"aud": "archivist"})
    monkeypatch.setattr(inprocess, "refusal", lambda *a, **k: None)
    return client


async def _drain(client: inprocess.InProcessBrokerClient) -> None:
    async for _ in client.send_message("archivist", "hi"):
        pass


@pytest.mark.asyncio
async def test_a_guardrail_refusal_says_it_is_one(monkeypatch) -> None:
    client = _client(monkeypatch, GuardrailBlocked("router guardrail blocked: budget"))
    with pytest.raises(BrokerError) as exc_info:
        await _drain(client)
    msg = str(exc_info.value)
    assert msg.startswith("refused by guardrail"), msg
    assert "retrying will not help" in msg
    assert "budget" in msg
    assert isinstance(exc_info.value.__cause__, GuardrailBlocked)


@pytest.mark.asyncio
async def test_other_dispatch_errors_keep_the_generic_message(monkeypatch) -> None:
    client = _client(monkeypatch, DispatchError("no agent registered: 'x'"))
    with pytest.raises(BrokerError, match=r"^dispatch error: no agent registered"):
        await _drain(client)


@pytest.mark.asyncio
async def test_a_boot_that_fails_after_startup_shuts_the_app_down(monkeypatch) -> None:
    """cabal#36: the TUI no longer calls __aexit__ after a failed __aenter__,
    so a failure after the lifespan started must close it here."""
    import contextlib

    events: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any):
        events.append("start")
        yield
        events.append("stop")

    fake_app = SimpleNamespace(
        router=SimpleNamespace(lifespan_context=lifespan),
        state=SimpleNamespace(dispatch_router=None, sessionmaker=None, master_key=b""),
    )

    @contextlib.asynccontextmanager
    async def fake_session(_sm: Any):
        yield None

    async def undecryptable(*_: Any, **__: Any) -> None:
        raise RuntimeError("cannot decrypt the signing key")

    monkeypatch.setattr(inprocess, "create_app", lambda **_: fake_app)
    monkeypatch.setattr(inprocess, "open_session", fake_session)
    monkeypatch.setattr(inprocess, "load_active_signing_key", undecryptable)

    client = inprocess.InProcessBrokerClient(operator_token="t")
    with pytest.raises(RuntimeError, match="cannot decrypt"):
        await client.__aenter__()
    assert events == ["start", "stop"]
    await client.__aexit__(None, None, None)  # a no-op now, not a second stop
    assert events == ["start", "stop"]
