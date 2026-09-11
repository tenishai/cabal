"""JWT minting and verification (caller identity proof).

Per §6.12 invariant 2: every dispatch — even in-process — carries a JWT
signed by Cabal Core's active signing key. The dispatch router verifies
that JWT against the JWKS before invoking the target handler.

Claims structure (minimal):
    iss   "cabal-core"
    sub   "<caller-agent-name>:<generation>"
    aud   "<target-agent-name>"
    iat   <epoch>
    exp   <epoch>  (5 minutes by default — see session_token doc for long
                    Tasks)
    cabal_persona   "<persona-name>"
    cabal_scope     ["dispatch", ...]
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from joserfc import jwt as jwt_lib
from joserfc.jwk import KeySet, RSAKey

from cabal.core.auth.keys import StoredKey

#: The ``sub`` prefix an OPERATOR token carries (cabal#154). Defined here, at
#: the module that mints them, because two copies of a string that grants an
#: authorization exemption is one copy too many.
OPERATOR_SUB_PREFIX = "operator:"

DEFAULT_TTL_SECONDS = 5 * 60
DEFAULT_ISS = "cabal-core"


@dataclass(slots=True)
class JWTClaims:
    iss: str
    sub: str
    aud: str
    iat: int
    exp: int
    cabal_persona: str | None = None
    cabal_scope: list[str] | None = None
    cabal_tool_extras: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "iss": self.iss,
            "sub": self.sub,
            "aud": self.aud,
            "iat": self.iat,
            "exp": self.exp,
        }
        if self.cabal_persona is not None:
            payload["cabal_persona"] = self.cabal_persona
        if self.cabal_scope is not None:
            payload["cabal_scope"] = self.cabal_scope
        if self.cabal_tool_extras:
            payload["cabal_tool_extras"] = self.cabal_tool_extras
        return payload


class JWTError(RuntimeError):
    """Raised on any verification failure (signature, claims, expiry)."""


def mint(
    *,
    signing_key: StoredKey,
    sub: str,
    aud: str,
    persona: str | None = None,
    scope: list[str] | None = None,
    tool_extras: dict[str, Any] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    issuer: str = DEFAULT_ISS,
    now: int | None = None,
) -> str:
    """Mint a JWT signed by `signing_key`.

    `tool_extras` becomes the `cabal_tool_extras` claim — liminal forwards
    it into Context.extra so handlers can read per-persona scoping
    config. Defaults to None / omitted from payload when not provided.
    """
    now_int = now if now is not None else int(time.time())
    claims = JWTClaims(
        iss=issuer,
        sub=sub,
        aud=aud,
        iat=now_int,
        exp=now_int + ttl_seconds,
        cabal_persona=persona,
        cabal_scope=scope,
        cabal_tool_extras=tool_extras,
    )
    private_jwk = RSAKey.import_key(signing_key.private_key)
    header = {"alg": "RS256", "kid": signing_key.kid, "typ": "JWT"}
    return jwt_lib.encode(header, claims.to_payload(), private_jwk)


def verify(
    token: str,
    *,
    key_set: KeySet,
    expected_audience: str | None = None,
    expected_issuer: str = DEFAULT_ISS,
    leeway_seconds: int = 30,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify signature + claims; return the decoded payload.

    Raises JWTError on any failure (signature, audience mismatch, expired).
    """
    try:
        # Pin the algorithm explicitly. Without this, joserfc accepts any
        # algorithm compatible with a key in the set, so the "RS256 only"
        # invariant would rely implicitly on the key set holding only RSA
        # keys — one key-type addition (or an alg-forgery like HS256 wearing
        # a legit kid) away from silently weakening. See cabal#75.
        # COUPLED with mint()'s hard-coded RS256 header above: a future
        # rotation to a different alg (RS384/512, EdDSA) must update BOTH
        # sites, or verification will silently reject freshly-minted tokens.
        decoded = jwt_lib.decode(token, key_set, algorithms=["RS256"])
    except Exception as exc:  # noqa: BLE001
        raise JWTError(f"signature verification failed: {exc}") from exc

    claims = decoded.claims
    now_int = now if now is not None else int(time.time())

    if claims.get("iss") != expected_issuer:
        raise JWTError(f"unexpected iss: {claims.get('iss')!r}")

    if expected_audience is not None and claims.get("aud") != expected_audience:
        raise JWTError(
            f"audience mismatch: token aud={claims.get('aud')!r}, "
            f"expected {expected_audience!r}"
        )

    exp = claims.get("exp")
    if exp is None or now_int > int(exp) + leeway_seconds:
        raise JWTError("token expired")

    iat = claims.get("iat")
    if iat is None or int(iat) > now_int + leeway_seconds:
        raise JWTError("token issued in the future")

    return dict(claims)


def extract_bearer(authorization: str | None) -> str:
    """Extract the raw token from an Authorization: Bearer <token> header.

    Raises HTTPException(401) if the header is absent or malformed.
    Import from here so routes don't each define their own copy.
    """
    from fastapi import HTTPException

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing or malformed Authorization header",
        )
    return authorization.split(" ", 1)[1].strip()
