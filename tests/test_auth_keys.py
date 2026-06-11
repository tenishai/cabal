"""Signing-key tests."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.auth.keys import (
    create_signing_key,
    list_public_jwks,
    load_active_signing_key,
    pem_to_private,
    private_to_pem,
)
from cabal.core.auth.master_key import (
    MasterKeyError,
    resolve_master_key,
)
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base, SigningKey


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


def test_resolve_master_key_from_env(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CABAL_MASTER_KEY", key)
    resolved = resolve_master_key()
    assert resolved == key.encode("ascii")


def test_resolve_master_key_rejects_unset_in_prod(monkeypatch) -> None:
    monkeypatch.delenv("CABAL_MASTER_KEY", raising=False)
    with pytest.raises(MasterKeyError, match="CABAL_MASTER_KEY is unset"):
        resolve_master_key(allow_dev_fallback=False)


def test_resolve_master_key_dev_fallback_generates_and_reuses(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CABAL_MASTER_KEY", raising=False)
    fallback = tmp_path / "master.key"
    k1 = resolve_master_key(
        dev_fallback_path=fallback, allow_dev_fallback=True
    )
    assert fallback.exists()
    # Permissions: 0600
    mode = fallback.stat().st_mode & 0o777
    assert mode == 0o600
    k2 = resolve_master_key(
        dev_fallback_path=fallback, allow_dev_fallback=True
    )
    assert k1 == k2  # reuse, not regen


def test_resolve_master_key_rejects_bad_base64(monkeypatch) -> None:
    monkeypatch.setenv("CABAL_MASTER_KEY", "not!valid!base64")
    with pytest.raises(MasterKeyError):
        resolve_master_key()


def test_pem_roundtrip() -> None:
    from cabal.core.auth.keys import generate_rsa_keypair

    priv, _pub = generate_rsa_keypair()
    pem = private_to_pem(priv)
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    rt = pem_to_private(pem)
    # Both keys have the same modulus
    assert rt.public_key().public_numbers().n == priv.public_key().public_numbers().n


@pytest.mark.asyncio
async def test_create_and_load_active_key(
    sessionmaker: async_sessionmaker[AsyncSession], master_key: bytes
) -> None:
    async with sessionmaker() as session:
        created = await create_signing_key(session, master_key=master_key)
        await session.commit()
        assert created.kid.startswith("cabal-")
        assert created.public_jwk["alg"] == "RS256"
        assert created.public_jwk["use"] == "sig"

    async with sessionmaker() as session:
        loaded = await load_active_signing_key(session, master_key=master_key)
        assert loaded is not None
        assert loaded.kid == created.kid
        assert (
            loaded.private_key.public_key().public_numbers().n
            == created.private_key.public_key().public_numbers().n
        )


@pytest.mark.asyncio
async def test_load_active_returns_none_when_empty(
    sessionmaker: async_sessionmaker[AsyncSession], master_key: bytes
) -> None:
    async with sessionmaker() as session:
        assert await load_active_signing_key(session, master_key=master_key) is None


@pytest.mark.asyncio
async def test_list_public_jwks_excludes_dropped_keys(
    sessionmaker: async_sessionmaker[AsyncSession], master_key: bytes
) -> None:
    async with sessionmaker() as session:
        first = await create_signing_key(session, master_key=master_key)
        second = await create_signing_key(session, master_key=master_key)
        # mark first as retired
        row = await session.get(SigningKey, first.kid)
        assert row is not None
        row.status = "retired"
        await session.commit()

    async with sessionmaker() as session:
        jwks = await list_public_jwks(session)
        kids = {j["kid"] for j in jwks}
        assert kids == {first.kid, second.kid}


@pytest.mark.asyncio
async def test_jwk_n_decodes_to_modulus(
    sessionmaker: async_sessionmaker[AsyncSession], master_key: bytes
) -> None:
    """The `n` field of the JWK must equal the public key modulus."""
    async with sessionmaker() as session:
        created = await create_signing_key(session, master_key=master_key)

    n_raw = created.public_jwk["n"]
    # JWK base64url uses no padding
    padding = "=" * (-len(n_raw) % 4)
    decoded = base64.urlsafe_b64decode(n_raw + padding)
    n_int = int.from_bytes(decoded, "big")
    assert n_int == created.private_key.public_key().public_numbers().n
