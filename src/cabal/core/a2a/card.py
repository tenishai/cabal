"""Sign and verify AgentCards as JWS (RFC 7515 compact).

A signed AgentCard is a JWS whose payload is the JCS-canonicalized JSON
of the AgentCard. Signing uses the active RS256 key; verification looks
up the `kid` in the issuer's JWKS.

Cabal §6.12 invariant 1: verification is cached per (kid, content_hash)
with a 5-minute TTL; rotation invalidates the cache.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from joserfc import jws
from joserfc.jwk import KeySet, RSAKey

from cabal.core.a2a.types import AgentCard
from cabal.core.auth.keys import StoredKey

CACHE_TTL_SECONDS = 5 * 60


def jcs_canonicalize(obj: object) -> bytes:
    """Minimal JCS (RFC 8785) canonicalization.

    Python's json.dumps with sort_keys=True and the right separators is
    sufficient for objects with only string keys, numbers, bools, null,
    arrays, and nested objects (the AgentCard shape). It does NOT handle
    full RFC 8785 number normalization or unpaired surrogates — that's a
    Phase 2 concern when we cross-verify with other implementations.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _content_hash(canonical: bytes) -> str:
    return hashlib.sha256(canonical).hexdigest()


def sign_card(card: AgentCard, signing_key: StoredKey) -> str:
    """Return a JWS compact serialization of `card` signed by `signing_key`."""
    canonical = jcs_canonicalize(card.model_dump(by_alias=True, exclude_none=True))
    private_jwk = RSAKey.import_key(signing_key.private_key)
    header = {"alg": "RS256", "kid": signing_key.kid, "typ": "agent-card+jws"}
    return jws.serialize_compact(header, canonical, private_jwk)


@dataclass(slots=True)
class VerifiedCard:
    card: AgentCard
    kid: str
    content_hash: str


class AgentCardVerifier:
    """Caches verified AgentCards per (kid, content_hash) with a TTL.

    The cache is keyed on content so a fresh signature over the same bytes
    is a free lookup; a rotated `kid` (different key for the same content)
    is a fresh verification. `invalidate_kid(kid)` busts everything signed
    by that key after rotation.
    """

    def __init__(self, *, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[tuple[str, str], tuple[VerifiedCard, float]] = {}

    def invalidate_kid(self, kid: str) -> None:
        self._cache = {k: v for k, v in self._cache.items() if k[0] != kid}

    def invalidate_all(self) -> None:
        self._cache.clear()

    def verify(self, jws_compact: str, key_set: KeySet) -> VerifiedCard:
        """Verify a JWS-wrapped AgentCard, returning the parsed card.

        Raises:
            joserfc.errors.BadSignatureError: signature did not validate
            ValueError: payload is not a valid AgentCard
        """
        sig = jws.extract_compact(jws_compact.encode("ascii"))
        kid = sig.protected.get("kid")
        if not kid:
            raise ValueError("JWS protected header missing kid")
        content_hash = _content_hash(sig.payload)

        cached = self._cache.get((kid, content_hash))
        if cached is not None:
            verified, expires_at = cached
            if expires_at > time.monotonic():
                return verified
            del self._cache[(kid, content_hash)]

        # Full verify against the JWKS.
        jws.deserialize_compact(jws_compact, key_set)
        card = AgentCard.model_validate(json.loads(sig.payload))
        verified = VerifiedCard(card=card, kid=kid, content_hash=content_hash)
        self._cache[(kid, content_hash)] = (verified, time.monotonic() + self._ttl)
        return verified

    def cache_size(self) -> int:
        return len(self._cache)
