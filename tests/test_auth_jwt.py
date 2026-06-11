"""JWT mint + verify tests."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.auth.jwt import JWTError, mint, verify
from cabal.core.auth.keys import create_signing_key
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest_asyncio.fixture
async def stored_key(sessionmaker) -> tuple:
    master = Fernet.generate_key()
    async with sessionmaker() as s:
        stored = await create_signing_key(s, master_key=master)
        await s.commit()
    public_jwk_dict = dict(
        RSAKey.import_key(stored.private_key.public_key()).as_dict(private=False)
    )
    public_jwk_dict["kid"] = stored.kid
    key_set = KeySet.import_key_set({"keys": [public_jwk_dict]})
    return stored, key_set


def test_mint_and_verify_roundtrip(stored_key) -> None:
    stored, key_set = stored_key
    token = mint(
        signing_key=stored,
        sub="rook-primus:1",
        aud="archivist",
        persona="rook",
        scope=["dispatch"],
    )
    payload = verify(token, key_set=key_set, expected_audience="archivist")
    assert payload["sub"] == "rook-primus:1"
    assert payload["aud"] == "archivist"
    assert payload["cabal_persona"] == "rook"
    assert payload["cabal_scope"] == ["dispatch"]


def test_audience_mismatch_rejected(stored_key) -> None:
    stored, key_set = stored_key
    token = mint(signing_key=stored, sub="rook-primus:1", aud="archivist")
    with pytest.raises(JWTError, match="audience mismatch"):
        verify(token, key_set=key_set, expected_audience="librarian")


def test_expired_token_rejected(stored_key) -> None:
    stored, key_set = stored_key
    past = int(time.time()) - 600
    token = mint(
        signing_key=stored,
        sub="rook-primus:1",
        aud="archivist",
        ttl_seconds=60,
        now=past,
    )
    with pytest.raises(JWTError, match="expired"):
        verify(token, key_set=key_set, expected_audience="archivist")


def test_audience_omitted_skips_check(stored_key) -> None:
    """When the caller doesn't pin an audience, the token still validates
    on signature + exp."""
    stored, key_set = stored_key
    token = mint(signing_key=stored, sub="rook-primus:1", aud="archivist")
    payload = verify(token, key_set=key_set, expected_audience=None)
    assert payload["aud"] == "archivist"


def test_wrong_issuer_rejected(stored_key) -> None:
    stored, key_set = stored_key
    token = mint(
        signing_key=stored, sub="rook-primus:1", aud="archivist", issuer="other-issuer"
    )
    with pytest.raises(JWTError, match="unexpected iss"):
        verify(token, key_set=key_set, expected_audience="archivist")


def test_signature_fail_on_wrong_keyset(stored_key) -> None:
    stored, _key_set = stored_key
    token = mint(signing_key=stored, sub="rook-primus:1", aud="archivist")

    # A different keyset (no matching kid) should fail verification.
    from cabal.core.auth.keys import generate_rsa_keypair

    other_priv, _ = generate_rsa_keypair()
    other_jwk_dict = dict(
        RSAKey.import_key(other_priv.public_key()).as_dict(private=False)
    )
    other_jwk_dict["kid"] = "different-kid"
    bad_set = KeySet.import_key_set({"keys": [other_jwk_dict]})
    with pytest.raises(JWTError):
        verify(token, key_set=bad_set, expected_audience="archivist")


# ---- Phase 1 audience enforcement tests ------------------------------------


def test_audience_enforced_on_operator_route(stored_key) -> None:
    """A token minted for a different aud is rejected from operator routes."""
    stored, key_set = stored_key
    # Mint with aud="limenal" (a service token) then try to use as operator
    token = mint(
        signing_key=stored,
        sub="service:limenal",
        aud="limenal",
        scope=["operator"],  # elevated scope but wrong audience
    )
    with pytest.raises(JWTError, match="audience mismatch"):
        verify(token, key_set=key_set, expected_audience="cabal-core")


def test_secrets_fetch_accepts_limenal_aud(stored_key) -> None:
    """A limenal service token (aud=limenal) can reach the secrets endpoint."""
    stored, key_set = stored_key
    token = mint(
        signing_key=stored,
        sub="service:limenal",
        aud="limenal",
        scope=["secrets-fetch"],
    )
    payload = verify(token, key_set=key_set, expected_audience="limenal")
    assert payload["sub"] == "service:limenal"
    assert "secrets-fetch" in payload["cabal_scope"]
