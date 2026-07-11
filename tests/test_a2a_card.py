"""AgentCard sign + verify + cache tests."""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from joserfc import jws
from joserfc.errors import BadSignatureError, UnsupportedAlgorithmError
from joserfc.jwk import KeySet, OctKey, RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.a2a.card import (
    AgentCardVerifier,
    VerifiedCard,
    jcs_canonicalize,
    sign_card,
)
from cabal.core.a2a.types import AgentCard, Skill
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


@pytest.fixture
def master_key() -> bytes:
    return Fernet.generate_key()


@pytest.fixture
def sample_card() -> AgentCard:
    return AgentCard(
        name="rook-primus",
        url="http://rook-primus:7400",
        version="0.1.0",
        skills=[Skill(id="review", name="Code Review")],
        metadata={"cabal_generation": 1, "cabal_safety_tier": "strict"},
    )


def test_jcs_canonicalize_is_stable() -> None:
    a = {"b": 1, "a": 2, "c": {"y": 1, "x": 2}}
    b = {"c": {"x": 2, "y": 1}, "a": 2, "b": 1}
    assert jcs_canonicalize(a) == jcs_canonicalize(b)


def test_jcs_canonicalize_compact_separators() -> None:
    assert jcs_canonicalize({"a": 1, "b": 2}) == b'{"a":1,"b":2}'


@pytest.mark.asyncio
async def test_sign_and_verify_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    master_key: bytes,
    sample_card: AgentCard,
) -> None:
    async with sessionmaker() as session:
        stored = await create_signing_key(session, master_key=master_key)
        await session.commit()

    token = sign_card(sample_card, stored)
    assert token.count(".") == 2  # JWS compact = header.payload.signature

    public_jwk = RSAKey.import_key(stored.private_key.public_key())
    public_jwk.ensure_kid()
    # Force kid to match what's in the header
    public_jwk_dict = dict(public_jwk.as_dict(private=False))
    public_jwk_dict["kid"] = stored.kid
    key_set = KeySet.import_key_set({"keys": [public_jwk_dict]})

    verifier = AgentCardVerifier()
    verified: VerifiedCard = verifier.verify(token, key_set)
    assert verified.card.name == "rook-primus"
    assert verified.kid == stored.kid


@pytest.mark.asyncio
async def test_verify_rejects_tampered_payload(
    sessionmaker: async_sessionmaker[AsyncSession],
    master_key: bytes,
    sample_card: AgentCard,
) -> None:
    async with sessionmaker() as session:
        stored = await create_signing_key(session, master_key=master_key)
        await session.commit()

    token = sign_card(sample_card, stored)
    header, payload, sig = token.split(".")
    # Flip a byte in the payload by replacing it with a different valid b64 segment
    import base64
    import json

    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["name"] = "evil-impersonator"
    tampered_payload = (
        base64.urlsafe_b64encode(jcs_canonicalize(decoded)).rstrip(b"=").decode("ascii")
    )
    tampered = f"{header}.{tampered_payload}.{sig}"

    public_jwk_dict = dict(RSAKey.import_key(stored.private_key.public_key()).as_dict(private=False))
    public_jwk_dict["kid"] = stored.kid
    key_set = KeySet.import_key_set({"keys": [public_jwk_dict]})

    verifier = AgentCardVerifier()
    with pytest.raises(BadSignatureError):
        verifier.verify(tampered, key_set)


@pytest.mark.asyncio
async def test_verify_rejects_hs256_forgery(
    sessionmaker: async_sessionmaker[AsyncSession],
    master_key: bytes,
    sample_card: AgentCard,
) -> None:
    """An HS256-forged card must be rejected even when a symmetric key is in
    the JWKS. sign_card() hard-codes RS256, so without the algorithms pin on
    deserialize_compact an oct key entering the key set lets an attacker forge
    an HS256-signed card and have it validate. Sibling of
    test_auth_jwt.py::test_hs256_forgery_rejected. Regression for cabal#83.
    """
    async with sessionmaker() as session:
        stored = await create_signing_key(session, master_key=master_key)
        await session.commit()

    # Legit RSA key + an intruder symmetric key sharing the JWKS — the
    # "one key-type addition away" condition.
    rsa_jwk = dict(RSAKey.import_key(stored.private_key.public_key()).as_dict(private=False))
    rsa_jwk["kid"] = stored.kid
    oct_key = OctKey.generate_key(256, {"kid": "oct-intruder"})
    key_set = KeySet.import_key_set({"keys": [rsa_jwk, dict(oct_key.as_dict())]})

    # Forge an HS256-signed card with the intruder key, wearing its kid.
    # Payload shape is irrelevant here: the algorithm gate short-circuits
    # before the payload is ever parsed, so this need not match sign_card()'s
    # canonical form (by_alias=True) — do NOT copy this as a signature-path test.
    payload = jcs_canonicalize(sample_card.model_dump(mode="json", exclude_none=True))
    forged = jws.serialize_compact(
        {"alg": "HS256", "kid": "oct-intruder", "typ": "agent-card+jws"},
        payload,
        oct_key,
        algorithms=["HS256"],  # the attacker imposes no RS256 restriction
    )

    verifier = AgentCardVerifier()
    with pytest.raises(UnsupportedAlgorithmError):
        verifier.verify(forged, key_set)


@pytest.mark.asyncio
async def test_verify_cache_hits_on_repeat(
    sessionmaker: async_sessionmaker[AsyncSession],
    master_key: bytes,
    sample_card: AgentCard,
) -> None:
    async with sessionmaker() as session:
        stored = await create_signing_key(session, master_key=master_key)
        await session.commit()

    token = sign_card(sample_card, stored)
    public_jwk_dict = dict(RSAKey.import_key(stored.private_key.public_key()).as_dict(private=False))
    public_jwk_dict["kid"] = stored.kid
    key_set = KeySet.import_key_set({"keys": [public_jwk_dict]})

    verifier = AgentCardVerifier()
    assert verifier.cache_size() == 0
    verifier.verify(token, key_set)
    assert verifier.cache_size() == 1
    verifier.verify(token, key_set)
    assert verifier.cache_size() == 1  # still 1, hit the cache


@pytest.mark.asyncio
async def test_invalidate_kid_busts_cache(
    sessionmaker: async_sessionmaker[AsyncSession],
    master_key: bytes,
    sample_card: AgentCard,
) -> None:
    async with sessionmaker() as session:
        stored = await create_signing_key(session, master_key=master_key)
        await session.commit()

    token = sign_card(sample_card, stored)
    public_jwk_dict = dict(RSAKey.import_key(stored.private_key.public_key()).as_dict(private=False))
    public_jwk_dict["kid"] = stored.kid
    key_set = KeySet.import_key_set({"keys": [public_jwk_dict]})

    verifier = AgentCardVerifier()
    verifier.verify(token, key_set)
    assert verifier.cache_size() == 1
    verifier.invalidate_kid(stored.kid)
    assert verifier.cache_size() == 0
