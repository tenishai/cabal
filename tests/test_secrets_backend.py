"""SecretsBackend Protocol tests.

Roundtrip exercise of every Protocol method against the default
SqliteFernetBackend. When new backends land (keychain, Vault, etc.), they
should be parametrized into the same fixtures so this test suite covers them.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.models import Base
from cabal.core.secrets_backend import (
    SecretEntry,
    SecretsBackend,
    SqliteFernetBackend,
)


@pytest.fixture
async def backend(tmp_path):
    """SqliteFernetBackend backed by a per-test in-memory DB."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'secrets.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = make_sessionmaker(engine)
    master_key = Fernet.generate_key()
    yield SqliteFernetBackend(sessionmaker=sessionmaker, master_key=master_key)
    await engine.dispose()


@pytest.mark.asyncio
async def test_backend_satisfies_protocol(backend) -> None:
    """SqliteFernetBackend duck-types to SecretsBackend at runtime."""
    assert isinstance(backend, SecretsBackend)


@pytest.mark.asyncio
async def test_set_get_roundtrip(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "xoxb-real-secret")
    creds = await backend.get("mason", "slack")
    assert creds == {"TOKEN": "xoxb-real-secret"}


@pytest.mark.asyncio
async def test_get_empty_when_nothing_stored(backend) -> None:
    creds = await backend.get("ghost", "vanished")
    assert creds == {}


@pytest.mark.asyncio
async def test_set_overwrites_existing(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "first")
    await backend.set("mason", "slack", "TOKEN", "second")
    creds = await backend.get("mason", "slack")
    assert creds == {"TOKEN": "second"}


@pytest.mark.asyncio
async def test_set_with_purpose(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "v", purpose="prod webhook")
    entries = await backend.list_entries(persona="mason", handler="slack")
    assert entries == [SecretEntry(persona="mason", handler="slack", key="TOKEN")]


@pytest.mark.asyncio
async def test_unset_returns_true_when_existed(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "v")
    existed = await backend.unset("mason", "slack", "TOKEN")
    assert existed is True
    assert await backend.get("mason", "slack") == {}


@pytest.mark.asyncio
async def test_unset_returns_false_when_absent(backend) -> None:
    existed = await backend.unset("ghost", "vanished", "MISSING")
    assert existed is False


@pytest.mark.asyncio
async def test_list_entries_unfiltered(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "v1")
    await backend.set("mason", "github", "PAT", "v2")
    await backend.set("archivist", "slack", "TOKEN", "v3")
    entries = await backend.list_entries()
    assert {(e.persona, e.handler, e.key) for e in entries} == {
        ("mason", "slack", "TOKEN"),
        ("mason", "github", "PAT"),
        ("archivist", "slack", "TOKEN"),
    }


@pytest.mark.asyncio
async def test_list_entries_filtered_by_persona(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "v1")
    await backend.set("archivist", "slack", "TOKEN", "v2")
    entries = await backend.list_entries(persona="mason")
    assert [e.persona for e in entries] == ["mason"]


@pytest.mark.asyncio
async def test_list_entries_filtered_by_persona_and_handler(backend) -> None:
    await backend.set("mason", "slack", "TOKEN_A", "v1")
    await backend.set("mason", "slack", "TOKEN_B", "v2")
    await backend.set("mason", "github", "PAT", "v3")
    entries = await backend.list_entries(persona="mason", handler="slack")
    assert {e.key for e in entries} == {"TOKEN_A", "TOKEN_B"}


@pytest.mark.asyncio
async def test_purge_for_returns_deleted_count(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "v1")
    await backend.set("mason", "slack", "SIGNING_KEY", "v2")
    await backend.set("mason", "github", "PAT", "v3")
    count = await backend.purge_for("mason", "slack")
    assert count == 2
    assert await backend.get("mason", "slack") == {}
    assert await backend.get("mason", "github") == {"PAT": "v3"}


@pytest.mark.asyncio
async def test_multiple_keys_for_same_handler(backend) -> None:
    await backend.set("mason", "slack", "TOKEN", "xoxb-v1")
    await backend.set("mason", "slack", "SIGNING_SECRET", "ss-v1")
    creds = await backend.get("mason", "slack")
    assert creds == {"TOKEN": "xoxb-v1", "SIGNING_SECRET": "ss-v1"}
