"""Phase NN end-to-end: identity.toml allowed_handlers reaches limenal.

`[personas.X.mcp].allowed_handlers = ["memory"]` folds into the JWT's
`cabal_tool_extras["limenal.allowed_handlers"]` claim at mint time.
limenal filters tools/list + refuses tools/call accordingly.

The limenal-side enforcement is covered by limenal's own
test_handler_allowlist.py — this test verifies the cabal side puts the
right values in the right places.
"""
from __future__ import annotations

import pytest

pytest.importorskip("limenal")


from textwrap import dedent

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

# ---- direct: mint(..., tool_extras={"limenal.allowed_handlers": [...]}) -


@pytest.mark.asyncio
async def test_mint_with_allowed_handlers_claim() -> None:
    """Lowest-level: mint() embeds whatever tool_extras dict it was given."""
    from cryptography.hazmat.primitives import serialization

    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import StoredKey, generate_rsa_keypair

    priv, _ = generate_rsa_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    rsa = RSAKey.import_key(pem)
    rsa.ensure_kid()
    public_jwk = dict(rsa.as_dict(private=False))
    public_jwk["kid"] = rsa.kid
    stored = StoredKey(
        kid=rsa.kid, private_key=priv, public_jwk=public_jwk, status="active"
    )

    token = mint(
        signing_key=stored,
        sub="archivist",
        aud="limenal",
        persona="archivist",
        scope=["dispatch"],
        tool_extras={"limenal.allowed_handlers": ["memory", "notify"]},
    )
    key_set = KeySet.import_key_set({"keys": [public_jwk]})
    decoded = jwt.decode(token, key_set)
    claims = dict(decoded.claims)
    assert claims["cabal_tool_extras"]["limenal.allowed_handlers"] == [
        "memory",
        "notify",
    ]


# ---- identity.toml → handler.tool_proxy.token_provider() ------------------


@pytest.mark.asyncio
async def test_identity_allowed_handlers_flows_into_jwt(tmp_path) -> None:
    """`[personas.X.mcp].allowed_handlers` in identity.toml lands in the
    cabal_tool_extras claim of the JWT each persona mints."""
    import os
    from pathlib import Path

    from cabal.core.app import create_app

    identity_body = dedent(
        """
        [limenal]
        url = "http://limenal-test"

        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"

        [personas.archivist.mcp]
        enabled = true
        allowed_handlers = ["memory", "notify"]
        """
    )
    identity_path = tmp_path / "identity.toml"
    identity_path.write_text(identity_body)
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))

    async def fn(*a, **kw):
        return {"choices": [{"message": {"content": "x"}}]}

    app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )
    async with app.router.lifespan_context(app):
        archivist = app.state.dispatch_router._local["archivist"]  # type: ignore[attr-defined]
        # Capture the token the proxy's provider mints
        token = archivist.tool_proxy.client._token()  # private, but test-stable

        # Verify against limenal-side expectations: persona claim is
        # archivist, allowed_handlers contains memory + notify, no extra
        # entries.
        key_set = app.state.jwks_provider()
        decoded = jwt.decode(token, key_set)
        claims = dict(decoded.claims)
        assert claims["cabal_persona"] == "archivist"
        assert claims["cabal_tool_extras"]["limenal.allowed_handlers"] == [
            "memory",
            "notify",
        ]


@pytest.mark.asyncio
async def test_identity_unrestricted_persona_omits_allowlist_claim(
    tmp_path,
) -> None:
    """No allowed_handlers → cabal_tool_extras has no allowlist key.
    This keeps the JWT lean for the common 'persona can use everything'
    case."""
    import os
    from pathlib import Path

    from cabal.core.app import create_app

    identity_body = dedent(
        """
        [limenal]
        url = "http://limenal-test"

        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"

        [personas.archivist.mcp]
        enabled = true
        # No allowed_handlers → unrestricted
        """
    )
    identity_path = tmp_path / "identity.toml"
    identity_path.write_text(identity_body)
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))

    async def fn(*a, **kw):
        return {"choices": [{"message": {"content": "x"}}]}

    app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )
    async with app.router.lifespan_context(app):
        archivist = app.state.dispatch_router._local["archivist"]  # type: ignore[attr-defined]
        token = archivist.tool_proxy.client._token()
        key_set = app.state.jwks_provider()
        decoded = jwt.decode(token, key_set)
        claims = dict(decoded.claims)
        # Either there's no tool_extras claim at all, or it doesn't carry
        # the allowed_handlers key.
        extras = claims.get("cabal_tool_extras") or {}
        assert "limenal.allowed_handlers" not in extras


@pytest.mark.asyncio
async def test_identity_empty_allowed_handlers_is_forwarded(tmp_path) -> None:
    """An explicit empty list is meaningful (revoke everything) — it
    must flow through, not get treated as None."""
    import os
    from pathlib import Path

    from cabal.core.app import create_app

    identity_body = dedent(
        """
        [limenal]
        url = "http://limenal-test"

        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"

        [personas.archivist.mcp]
        enabled = true
        allowed_handlers = []
        """
    )
    identity_path = tmp_path / "identity.toml"
    identity_path.write_text(identity_body)
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))

    async def fn(*a, **kw):
        return {"choices": [{"message": {"content": "x"}}]}

    app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )
    async with app.router.lifespan_context(app):
        archivist = app.state.dispatch_router._local["archivist"]  # type: ignore[attr-defined]
        token = archivist.tool_proxy.client._token()
        key_set = app.state.jwks_provider()
        decoded = jwt.decode(token, key_set)
        claims = dict(decoded.claims)
        assert claims["cabal_tool_extras"]["limenal.allowed_handlers"] == []


@pytest.mark.asyncio
async def test_tool_extras_takes_precedence_when_both_set(tmp_path) -> None:
    """If the operator sets allowed_handlers AND a different value
    under tool_extras["limenal.allowed_handlers"], the first-class
    field wins (consistent operator surface — the one we documented as
    canonical)."""
    import os
    from pathlib import Path

    from cabal.core.app import create_app

    identity_body = dedent(
        """
        [limenal]
        url = "http://limenal-test"

        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"

        [personas.archivist.mcp]
        enabled = true
        allowed_handlers = ["memory"]

        [personas.archivist.mcp.tool_extras]
        "limenal.allowed_handlers" = ["files", "web"]
        """
    )
    identity_path = tmp_path / "identity.toml"
    identity_path.write_text(identity_body)
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))

    async def fn(*a, **kw):
        return {"choices": [{"message": {"content": "x"}}]}

    app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=fn,
    )
    async with app.router.lifespan_context(app):
        archivist = app.state.dispatch_router._local["archivist"]  # type: ignore[attr-defined]
        token = archivist.tool_proxy.client._token()
        key_set = app.state.jwks_provider()
        decoded = jwt.decode(token, key_set)
        # first-class allowed_handlers overrides the tool_extras dict
        assert decoded.claims["cabal_tool_extras"]["limenal.allowed_handlers"] == [
            "memory"
        ]
