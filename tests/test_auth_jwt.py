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
        sub="mason-primus:1",
        aud="archivist",
        persona="mason",
        scope=["dispatch"],
    )
    payload = verify(token, key_set=key_set, expected_audience="archivist")
    assert payload["sub"] == "mason-primus:1"
    assert payload["aud"] == "archivist"
    assert payload["cabal_persona"] == "mason"
    assert payload["cabal_scope"] == ["dispatch"]


def test_audience_mismatch_rejected(stored_key) -> None:
    stored, key_set = stored_key
    token = mint(signing_key=stored, sub="mason-primus:1", aud="archivist")
    with pytest.raises(JWTError, match="audience mismatch"):
        verify(token, key_set=key_set, expected_audience="librarian")


def test_expired_token_rejected(stored_key) -> None:
    stored, key_set = stored_key
    past = int(time.time()) - 600
    token = mint(
        signing_key=stored,
        sub="mason-primus:1",
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
    token = mint(signing_key=stored, sub="mason-primus:1", aud="archivist")
    payload = verify(token, key_set=key_set, expected_audience=None)
    assert payload["aud"] == "archivist"


def test_wrong_issuer_rejected(stored_key) -> None:
    stored, key_set = stored_key
    token = mint(
        signing_key=stored, sub="mason-primus:1", aud="archivist", issuer="other-issuer"
    )
    with pytest.raises(JWTError, match="unexpected iss"):
        verify(token, key_set=key_set, expected_audience="archivist")


def test_signature_fail_on_wrong_keyset(stored_key) -> None:
    stored, _key_set = stored_key
    token = mint(signing_key=stored, sub="mason-primus:1", aud="archivist")

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
    # Mint with aud="liminal" (a service token) then try to use as operator
    token = mint(
        signing_key=stored,
        sub="service:liminal",
        aud="liminal",
        scope=["operator"],  # elevated scope but wrong audience
    )
    with pytest.raises(JWTError, match="audience mismatch"):
        verify(token, key_set=key_set, expected_audience="cabal-core")


def test_secrets_fetch_accepts_liminal_aud(stored_key) -> None:
    """A liminal service token (aud=liminal) can reach the secrets endpoint."""
    stored, key_set = stored_key
    token = mint(
        signing_key=stored,
        sub="service:liminal",
        aud="liminal",
        scope=["secrets-fetch"],
    )
    payload = verify(token, key_set=key_set, expected_audience="liminal")
    assert payload["sub"] == "service:liminal"
    assert "secrets-fetch" in payload["cabal_scope"]


# ---- Algorithm-confusion regression (cabal#75) -----------------------------


def test_hs256_forgery_rejected(stored_key) -> None:
    """An HS256 token must be rejected even if the keyset holds a symmetric key.

    The RS256 pin is defense-in-depth against the exact scenario cabal#75
    flags: "one key-type addition away." joserfc already refuses to use an
    RSA-typed key as an HMAC secret, so the classic public-key-as-secret
    attack is blocked by key typing regardless of the pin. The pin closes the
    *next* gap: the moment the JWKS gains any symmetric (oct) key — a future
    config change, a rotated service secret, a joserfc default change — an
    HS256 token signed with it would validate under an unpinned decode.

    This test proves the pin closes that path: it constructs a keyset holding
    the legitimate RSA key PLUS an oct key, forges an HS256 token with the oct
    key (valid iss/aud/iat/exp, so only the algorithm can reject it), and
    asserts verify() rejects it. Without the pin this forgery is ACCEPTED;
    with it, joserfc raises before any claim check.
    """
    from joserfc import jwt as jwt_lib
    from joserfc.jwk import KeySet, OctKey, RSAKey

    stored, _key_set = stored_key

    # Legit RSA key (as published), plus a symmetric key that has entered the
    # set — the "one key-type addition" the fix guards against.
    rsa_jwk = dict(RSAKey.import_key(stored.private_key.public_key()).as_dict(private=False))
    rsa_jwk["kid"] = stored.kid
    oct_key = OctKey.generate_key(256, {"kid": "oct-intruder"})
    key_set = KeySet.import_key_set({"keys": [rsa_jwk, dict(oct_key.as_dict())]})

    now = int(time.time())
    forged = jwt_lib.encode(
        {"alg": "HS256", "kid": "oct-intruder", "typ": "JWT"},
        {
            "iss": "cabal-core",
            "sub": "attacker:1",
            "aud": "archivist",
            "iat": now,
            "exp": now + 300,
            "cabal_scope": ["operator"],
        },
        oct_key,
        algorithms=["HS256"],  # the attacker imposes no RS256 restriction
    )

    with pytest.raises(JWTError):
        verify(forged, key_set=key_set, expected_audience="archivist")
