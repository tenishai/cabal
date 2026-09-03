"""RS256 keypair management.

Generates and persists signing keypairs in the `signing_keys` table.
Private keys are Fernet-encrypted with the master key (see master_key.py)
before being written. Public keys are stored as JWKs so the JWKS endpoint
can serve them without recomputing.

Keys carry a `kid` (key identifier) that goes into JWS protected headers
of AgentCards and JWTs. Rotation = mark old keys `retired`, generate
fresh `active`, broadcast invalidation event to all routers (§6.12 inv 1).
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cabal.core.auth.master_key import (
    EncryptedRowDamagedError,
    MasterKeyMismatchError,
    get_fernet,
    is_wellformed_fernet_token,
)
from cabal.core.models import SigningKey

KID_PREFIX = "cabal-"


def generate_kid() -> str:
    """A short, sortable, URL-safe kid."""
    return KID_PREFIX + secrets.token_hex(6)


def _public_key_to_jwk(public_key: RSAPublicKey, kid: str) -> dict[str, str]:
    numbers = public_key.public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": base64.urlsafe_b64encode(n).rstrip(b"=").decode("ascii"),
        "e": base64.urlsafe_b64encode(e).rstrip(b"=").decode("ascii"),
    }


def generate_rsa_keypair() -> tuple[RSAPrivateKey, RSAPublicKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def private_to_pem(private_key: RSAPrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def pem_to_private(pem: bytes) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError(f"expected RSA private key, got {type(key).__name__}")
    return key


@dataclass(slots=True)
class StoredKey:
    """A signing key as the rest of the system uses it."""

    kid: str
    private_key: RSAPrivateKey
    public_jwk: dict[str, str]
    status: str  # 'active' | 'retired'


async def create_signing_key(
    session: AsyncSession, *, master_key: bytes, kid: str | None = None
) -> StoredKey:
    """Generate a fresh RS256 keypair, persist encrypted, return it."""
    private_key, public_key = generate_rsa_keypair()
    kid = kid or generate_kid()
    public_jwk = _public_key_to_jwk(public_key, kid)
    encrypted_pem = get_fernet(master_key).encrypt(private_to_pem(private_key))

    row = SigningKey(
        kid=kid,
        public_jwk_json=public_jwk,
        private_key_pem_encrypted=encrypted_pem,
        status="active",
    )
    session.add(row)
    await session.flush()
    return StoredKey(kid=kid, private_key=private_key, public_jwk=public_jwk, status="active")


async def load_active_signing_key(
    session: AsyncSession, *, master_key: bytes
) -> StoredKey | None:
    """Most recently created active key; None if none exist."""
    result = await session.execute(
        select(SigningKey)
        .where(SigningKey.status == "active")
        .order_by(SigningKey.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    try:
        pem = get_fernet(master_key).decrypt(row.private_key_pem_encrypted)
    except InvalidToken as exc:
        # `InvalidToken` means EITHER wrong key OR damaged ciphertext, and the
        # remedies point opposite ways: restore a key, or accept the row is
        # gone. Check the envelope, which needs no key — a malformed one is
        # damage, full stop. A well-formed one is probably a wrong key, but
        # body-only damage looks identical, so that message names both rather
        # than promising the operator a key that may not exist (cabal#92).
        if not is_wellformed_fernet_token(row.private_key_pem_encrypted):
            raise EncryptedRowDamagedError(
                f"the stored ciphertext for signing key {row.kid!r} is not a "
                "well-formed Fernet token — the row is damaged, and no master "
                "key will decrypt it"
            ) from exc
        raise MasterKeyMismatchError(
            f"master key cannot decrypt signing key {row.kid!r}"
        ) from exc
    return StoredKey(
        kid=row.kid,
        private_key=pem_to_private(pem),
        public_jwk=row.public_jwk_json,
        status=row.status,
    )


async def list_public_jwks(session: AsyncSession) -> list[dict[str, str]]:
    """All keys we'd still accept signatures from (active + retired-but-cached).

    For JWKS endpoint exposure: callers should verify both active and the most
    recently retired key so an in-flight token signed under the old kid still
    validates during the rotation overlap window.
    """
    result = await session.execute(
        select(SigningKey).order_by(SigningKey.created_at.desc())
    )
    rows = result.scalars().all()
    return [r.public_jwk_json for r in rows if r.status in {"active", "retired"}]
