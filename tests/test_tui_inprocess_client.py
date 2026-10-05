"""Tests for InProcessBrokerClient.

Uses the same fixture patterns as test_personas_end_to_end.py:
  - conftest._isolate_env (autouse) sets CABAL_MASTER_KEY, CABAL_DB_URL,
    chdirs to tmp_path, and creates data/.
  - A stub acompletion that streams text back without hitting the network.

The TUI client boots the full Cabal app in-process (same event loop),
mints a valid operator token from the booted signing key, then asserts
that send_message() streams the stub's echoed reply.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

# Booting Cabal core in-process registers the archivist persona, which needs the
# sibling techne/liminal packages — auto-skip when they're absent (e.g. CI's
# no-siblings unit job), matching the convention in test_patterns_handler.py.
pytest.importorskip("techne")
pytest.importorskip("liminal")

from cabal.tui.client.base import BrokerClient, BrokerError  # noqa: E402
from cabal.tui.client.inprocess import InProcessBrokerClient  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _write_identity(tmp_path: Path) -> Path:
    """Write a minimal identity.toml declaring the archivist persona."""
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
"""
    )
    return p


async def stub_acompletion(model: str, messages: list[Any], **kwargs: Any) -> Any:
    """Streaming stub: echoes the last user message as a single SSE chunk.

    LiteLLMClient.astream calls this with stream=True and expects an
    async iterable of {"choices": [{"delta": {"content": ...}}]} dicts.
    """
    last_user = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    reply = f"echo: {last_user}"

    async def _stream():
        yield {"choices": [{"delta": {"content": reply}}]}

    return _stream()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_streams_stub_response(tmp_path: Path) -> None:
    """Happy path: send_message yields deltas containing the echoed text."""
    identity_path = _write_identity(tmp_path)

    # Pass a placeholder operator_token to bypass the None check in __init__.
    # We replace it with a real minted token after __aenter__.
    async with InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=stub_acompletion,
        operator_token="placeholder",
    ) as client:
        # Replace placeholder with a token minted from the booted signing key.
        client._operator_token = client._mint_operator_token()

        chunks: list[str] = []
        async for delta in client.send_message("archivist", "hello world"):
            chunks.append(delta)

    full = "".join(chunks)
    assert "hello world" in full, f"Expected 'hello world' in response, got: {full!r}"


@pytest.mark.asyncio
async def test_send_message_threads_in_session_history(tmp_path: Path) -> None:
    """A second message carries the first turn (user + agent) as history.

    Proves in-session memory: the persona sees what was said earlier in the
    same client, so multi-turn conversation works.
    """
    identity_path = _write_identity(tmp_path)

    calls: list[list[Any]] = []

    async def recording_stub(model: str, messages: list[Any], **kwargs: Any) -> Any:
        calls.append(messages)
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )

        async def _stream():
            yield {"choices": [{"delta": {"content": f"echo: {last_user}"}}]}

        return _stream()

    async with InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=recording_stub,
        operator_token="placeholder",
    ) as client:
        client._operator_token = client._mint_operator_token()

        async for _ in client.send_message("archivist", "my name is Richard"):
            pass
        async for _ in client.send_message("archivist", "what is my name?"):
            pass

    assert len(calls) >= 2, f"expected >=2 LLM calls (one per turn); got {len(calls)}"
    second = " ".join(str(m.get("content", "")) for m in calls[-1])
    # The first user turn AND the first agent reply must be present in turn 2.
    assert "my name is Richard" in second, f"prior user turn not threaded: {second!r}"
    assert "echo: my name is Richard" in second, f"prior agent turn not threaded: {second!r}"


@pytest.mark.asyncio
async def test_send_message_accumulates_multiple_chunks(tmp_path: Path) -> None:
    """send_message yields each chunk separately and they concatenate correctly."""
    identity_path = _write_identity(tmp_path)

    async def multi_chunk_stub(model: str, messages: list[Any], **kwargs: Any) -> Any:
        async def _stream():
            yield {"choices": [{"delta": {"content": "chunk1 "}}]}
            yield {"choices": [{"delta": {"content": "chunk2 "}}]}
            yield {"choices": [{"delta": {"content": "chunk3"}}]}

        return _stream()

    async with InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=multi_chunk_stub,
        operator_token="placeholder",
    ) as client:
        client._operator_token = client._mint_operator_token()

        chunks: list[str] = []
        async for delta in client.send_message("archivist", "ping"):
            chunks.append(delta)

    assert chunks == ["chunk1 ", "chunk2 ", "chunk3"]


@pytest.mark.asyncio
async def test_broker_client_protocol_satisfied(tmp_path: Path) -> None:
    """InProcessBrokerClient satisfies the BrokerClient Protocol at runtime."""
    identity_path = _write_identity(tmp_path)
    client = InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=stub_acompletion,
        operator_token="placeholder",
    )
    assert isinstance(client, BrokerClient), (
        "InProcessBrokerClient must satisfy the BrokerClient @runtime_checkable Protocol"
    )


def test_missing_operator_token_raises_broker_error(tmp_path: Path, monkeypatch) -> None:
    """BrokerError is raised at construction time when no token can be found."""
    identity_path = _write_identity(tmp_path)

    # Redirect DEFAULT_TOKEN_PATH to a non-existent file so TokenStore.load()
    # returns None.
    import cabal.cli.client as _cli_client

    monkeypatch.setattr(_cli_client, "DEFAULT_TOKEN_PATH", tmp_path / "no_token_here")

    with pytest.raises(BrokerError, match="cbl login"):
        InProcessBrokerClient(
            config_path=str(identity_path),
            acompletion_fn=stub_acompletion,
            # operator_token intentionally omitted
        )


@pytest.mark.asyncio
async def test_invalid_operator_token_raises_broker_error(tmp_path: Path) -> None:
    """send_message raises BrokerError when the operator token fails verification."""
    identity_path = _write_identity(tmp_path)

    async with InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=stub_acompletion,
        operator_token="not.a.valid.jwt",
    ) as client:
        with pytest.raises(BrokerError, match="operator token invalid"):
            async for _ in client.send_message("peer-archivist", "hi"):
                pass


@pytest.mark.asyncio
async def test_a_revoked_operator_token_raises_broker_error(tmp_path: Path) -> None:
    """The in-process client accepts a token too, so it honours subject
    revocations (PR #221 review)."""
    from cabal.core.auth.jwt import verify
    from cabal.core.auth.revocations import RevocationList

    identity_path = _write_identity(tmp_path)
    async with InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=stub_acompletion,
        operator_token="placeholder",
    ) as client:
        client._operator_token = client._mint_operator_token()
        sub = verify(client._operator_token, key_set=client._app.state.jwks_provider())["sub"]
        client._app.state.revocations = RevocationList({sub: int(time.time()) + 3600})
        with pytest.raises(BrokerError, match="revoked"):
            async for _ in client.send_message("peer-archivist", "hi"):
                pass


@pytest.mark.asyncio
async def test_db_url_from_env(tmp_path: Path) -> None:
    """InProcessBrokerClient picks up CABAL_DB_URL from the environment."""
    identity_path = _write_identity(tmp_path)

    # CABAL_DB_URL is already set by conftest._isolate_env; just confirm it works.
    assert os.environ.get("CABAL_DB_URL") is not None, "conftest should set CABAL_DB_URL"

    async with InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=stub_acompletion,
        # db_url omitted — should fall back to env
        operator_token="placeholder",
    ) as client:
        client._operator_token = client._mint_operator_token()
        chunks: list[str] = []
        async for delta in client.send_message("archivist", "env test"):
            chunks.append(delta)

    assert "env test" in "".join(chunks)


@pytest.mark.asyncio
async def test_default_persona_attribute(tmp_path: Path) -> None:
    """default_persona is accessible on the client instance."""
    identity_path = _write_identity(tmp_path)
    client = InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=stub_acompletion,
        operator_token="placeholder",
        default_persona="archivist",
    )
    assert client.default_persona == "archivist"


@pytest.mark.asyncio
async def test_send_message_to_a_federated_persona_mints_its_audience(tmp_path: Path) -> None:
    """cabal#234: the TUI's dispatch token for a federated persona carries the
    sovereign peer's federation_audience, not the bare persona name."""
    from cabal.core.auth.jwt import verify
    from cabal.core.bulletin import store as bulletin_store
    from cabal.core.models import Persona

    identity_path = _write_identity(tmp_path)
    async with InProcessBrokerClient(
        config_path=str(identity_path),
        acompletion_fn=stub_acompletion,
        operator_token="placeholder",
    ) as client:
        client._operator_token = client._mint_operator_token()
        # A persona of its own: archivist is already registered as a member at boot.
        async with client._app.state.sessionmaker() as s:
            s.add(Persona(name="peer-archivist", config_json={}))
            await s.flush()
            await bulletin_store.register_federated(
                s,
                name="peer-archivist",
                persona_name="peer-archivist",
                endpoint="https://archivist.rd.ts.net",
                federation_audience="archivist@rd",
                agent_card_json={"name": "archivist"},
                agent_card_kid="kid-fed",
            )
            await s.commit()

        seen: list[str] = []

        async def capture(**kwargs: Any):
            seen.append(kwargs["caller_jwt"])
            yield "ok"

        client._router.dispatch_streaming = capture
        async for _ in client.send_message("peer-archivist", "hi"):
            pass

        claims = verify(seen[0], key_set=client._app.state.jwks_provider(), expected_audience=None)
    assert claims["aud"] == "archivist@rd"
