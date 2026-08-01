"""Unit tests for cabal.core.secrets — encryption + naming + merge."""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base, Secret
from cabal.core.secrets import (
    SecretEntry,
    get_secrets_for,
    list_secret_entries,
    make_name,
    merge_with_env,
    parse_name,
    purge_secrets_for,
    set_secret,
    unset_secret,
)


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest.fixture
def master_key() -> bytes:
    return Fernet.generate_key()


# ---- name codec ---------------------------------------------------------


def test_make_name_uses_colon_separator() -> None:
    assert make_name("mason", "slack", "SLACK_BOT_TOKEN") == "mason:slack:SLACK_BOT_TOKEN"


def test_parse_name_roundtrip() -> None:
    name = make_name("mason", "slack", "SLACK_BOT_TOKEN")
    assert parse_name(name) == ("mason", "slack", "SLACK_BOT_TOKEN")


def test_parse_name_preserves_key_with_underscores() -> None:
    """The colon separator means multi-underscore KEYs round-trip cleanly."""
    name = make_name("archivist", "github", "GH_APP_PRIVATE_KEY")
    assert parse_name(name) == ("archivist", "github", "GH_APP_PRIVATE_KEY")


def test_parse_name_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        parse_name("nope")
    with pytest.raises(ValueError):
        parse_name("only:two")
    with pytest.raises(ValueError):
        parse_name(":empty:fields")


# ---- set / get roundtrip ------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_roundtrip(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        await set_secret(
            s,
            master_key=master_key,
            persona="mason",
            handler="slack",
            key="SLACK_BOT_TOKEN",
            value="xoxb-test",
        )
        await s.commit()

    async with sessionmaker() as s:
        creds = await get_secrets_for(
            s, master_key=master_key, persona="mason", handler="slack"
        )
    assert creds == {"SLACK_BOT_TOKEN": "xoxb-test"}


@pytest.mark.asyncio
async def test_set_is_encrypted_at_rest(sessionmaker, master_key) -> None:
    """The value column never contains the plaintext."""
    async with sessionmaker() as s:
        await set_secret(
            s,
            master_key=master_key,
            persona="mason",
            handler="slack",
            key="K",
            value="plaintext-secret",
        )
        await s.commit()

    async with sessionmaker() as s:
        row = await s.get(Secret, "mason:slack:K")
        assert row is not None
        assert b"plaintext-secret" not in row.value_encrypted


@pytest.mark.asyncio
async def test_set_overwrites_existing_value(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        await set_secret(
            s, master_key=master_key, persona="r", handler="h", key="K", value="v1"
        )
        await set_secret(
            s, master_key=master_key, persona="r", handler="h", key="K", value="v2"
        )
        await s.commit()

    async with sessionmaker() as s:
        creds = await get_secrets_for(
            s, master_key=master_key, persona="r", handler="h"
        )
    assert creds == {"K": "v2"}


@pytest.mark.asyncio
async def test_decryption_failure_skips_silently(sessionmaker, master_key) -> None:
    """A row encrypted under a different key shouldn't blow up — it gets
    silently skipped so a partial key rotation doesn't black-hole the
    whole secret set."""
    async with sessionmaker() as s:
        await set_secret(
            s, master_key=master_key, persona="r", handler="h", key="GOOD", value="ok"
        )
        # Insert a row encrypted with a stranger key
        other_key = Fernet.generate_key()
        garbage = Fernet(other_key).encrypt(b"different-key-can't-decrypt")
        s.add(Secret(name="r:h:BAD", value_encrypted=garbage))
        await s.commit()

    async with sessionmaker() as s:
        creds = await get_secrets_for(
            s, master_key=master_key, persona="r", handler="h"
        )
    assert creds == {"GOOD": "ok"}


# ---- per-(persona, handler) isolation ----------------------------------


@pytest.mark.asyncio
async def test_get_only_matches_persona_and_handler(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        await set_secret(
            s, master_key=master_key, persona="mason", handler="slack",
            key="MASON_SLACK", value="rs",
        )
        await set_secret(
            s, master_key=master_key, persona="raven", handler="slack",
            key="RAVEN_SLACK", value="vs",
        )
        await set_secret(
            s, master_key=master_key, persona="mason", handler="github",
            key="MASON_GH", value="rg",
        )
        await s.commit()

    async with sessionmaker() as s:
        creds = await get_secrets_for(
            s, master_key=master_key, persona="mason", handler="slack"
        )
    assert creds == {"MASON_SLACK": "rs"}


# ---- unset / purge -----------------------------------------------------


@pytest.mark.asyncio
async def test_unset_removes_secret(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        await set_secret(
            s, master_key=master_key, persona="r", handler="h", key="K", value="v"
        )
        existed = await unset_secret(s, persona="r", handler="h", key="K")
        assert existed is True
        existed_again = await unset_secret(s, persona="r", handler="h", key="K")
        assert existed_again is False
        await s.commit()

    async with sessionmaker() as s:
        creds = await get_secrets_for(
            s, master_key=master_key, persona="r", handler="h"
        )
    assert creds == {}


@pytest.mark.asyncio
async def test_purge_deletes_all_for_persona_handler(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        for k in ("A", "B", "C"):
            await set_secret(
                s, master_key=master_key, persona="r", handler="h", key=k, value="x"
            )
        await set_secret(
            s, master_key=master_key, persona="r", handler="other", key="A", value="x"
        )
        await s.commit()

    async with sessionmaker() as s:
        deleted = await purge_secrets_for(s, persona="r", handler="h")
        await s.commit()
    assert deleted == 3

    async with sessionmaker() as s:
        # Other handler's row untouched
        other = await get_secrets_for(
            s, master_key=master_key, persona="r", handler="other"
        )
    assert other == {"A": "x"}


# ---- list --------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_entries_no_values(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        await set_secret(
            s, master_key=master_key, persona="mason", handler="slack",
            key="SLACK_BOT_TOKEN", value="xoxb-1",
        )
        await set_secret(
            s, master_key=master_key, persona="raven", handler="github",
            key="GH_TOKEN", value="ghp-1",
        )
        await s.commit()

    async with sessionmaker() as s:
        entries = await list_secret_entries(s)
    assert len(entries) == 2
    assert all(isinstance(e, SecretEntry) for e in entries)
    # Sanity: no value field exists on SecretEntry — the list contract
    # is name-only by design.
    assert not hasattr(entries[0], "value")


@pytest.mark.asyncio
async def test_list_filters_by_persona(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        await set_secret(
            s, master_key=master_key, persona="mason", handler="slack",
            key="K", value="v",
        )
        await set_secret(
            s, master_key=master_key, persona="raven", handler="slack",
            key="K", value="v",
        )
        await s.commit()

    async with sessionmaker() as s:
        mason = await list_secret_entries(s, persona="mason")
        raven = await list_secret_entries(s, persona="raven")
    assert [e.persona for e in mason] == ["mason"]
    assert [e.persona for e in raven] == ["raven"]


@pytest.mark.asyncio
async def test_list_filters_by_handler(sessionmaker, master_key) -> None:
    async with sessionmaker() as s:
        await set_secret(
            s, master_key=master_key, persona="mason", handler="slack",
            key="K", value="v",
        )
        await set_secret(
            s, master_key=master_key, persona="mason", handler="github",
            key="K", value="v",
        )
        await s.commit()

    async with sessionmaker() as s:
        slack_only = await list_secret_entries(s, handler="slack")
    assert [e.handler for e in slack_only] == ["slack"]


# ---- env merge ---------------------------------------------------------


def test_merge_env_overrides_db() -> None:
    merged = merge_with_env(
        db_values={"A": "from-db", "B": "db-only"},
        env_values=[("A", "from-env"), ("C", "env-only")],
    )
    assert merged == {"A": "from-env", "B": "db-only", "C": "env-only"}


def test_merge_with_no_env_returns_db_unchanged() -> None:
    merged = merge_with_env(
        db_values={"A": "x", "B": "y"},
        env_values=[],
    )
    assert merged == {"A": "x", "B": "y"}
