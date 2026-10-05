"""A2A v1.0 AgentCards: RFC 8785, default removal, signatures, the card route (#270)."""

from __future__ import annotations

import base64
import copy
import json
import struct

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from joserfc.jwk import KeySet, RSAKey

from cabal.config.identity import CabalConfig, IdentityConfig, resolve_public_url
from cabal.core.a2a.v1_card import (
    AgentCardV1,
    CanonicalizationError,
    CardSignatureError,
    jcs,
    persona_card,
    sign_card,
    signed_card,
    signing_payload,
    verify_card,
)
from cabal.core.auth.keys import StoredKey, generate_rsa_keypair, private_to_pem
from cabal.core.bulletin import store as bulletin_store
from cabal.core.models import BootstrapCode, Persona


def _key(kid: str) -> StoredKey:
    private, _ = generate_rsa_keypair()
    jwk = RSAKey.import_key(private_to_pem(private))
    public = dict(jwk.as_dict(private=False))
    public["kid"] = kid
    return StoredKey(kid=kid, private_key=private, public_jwk=public, status="active")


def _key_set(*keys: StoredKey) -> KeySet:
    return KeySet.import_key_set({"keys": [k.public_jwk for k in keys]})


# ---- RFC 8785 ---------------------------------------------------------------------

# RFC 8785 Appendix B, Table 1 (IEEE 754 bits -> JSON).
_NUMBERS = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


@pytest.mark.parametrize("bits, expected", _NUMBERS)
def test_jcs_numbers_match_rfc_8785_appendix_b(bits: str, expected: str) -> None:
    (value,) = struct.unpack(">d", bytes.fromhex(bits))
    assert jcs(value).decode() == expected


def test_jcs_matches_the_rfc_8785_sample() -> None:
    """RFC 8785 section 3.2.2's input and its canonical form (section 3.2.3)."""
    backslash, quote = chr(92), chr(34)
    string = chr(0x20AC) + "$" + chr(0x0F) + chr(0x0A) + "A'B" + quote + backslash + backslash + quote + "/"
    sample = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        "string": string,
        "literals": [None, True, False],
    }
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"' + chr(0x20AC) + "$" + backslash + "u000f" + backslash + "nA'B"
        + backslash + quote + backslash * 4 + backslash + quote + '/"}'
    )
    assert jcs(sample).decode() == expected


def test_jcs_sorts_keys_by_utf16_code_units() -> None:
    # U+1F600 is the surrogate pair D83D DE00, which sorts before U+FB33 in
    # UTF-16 although its code point is larger.
    out = jcs({chr(0xFB33): 1, chr(0x1F600): 2, "b": 3, "a": 4}).decode()
    assert out == '{"a":4,"b":3,"' + chr(0x1F600) + '":2,"' + chr(0xFB33) + '":1}'


def test_jcs_integers_serialize_as_doubles() -> None:
    assert jcs([1, -3, 2**68, 10**21]).decode() == "[1,-3,295147905179352830000,1e+21]"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), chr(0xD800), {1: "x"}, b"x"])
def test_jcs_refuses_what_has_no_json_form(value) -> None:
    with pytest.raises(CanonicalizationError):
        jcs(value)


# ---- default removal and the signed content (spec 8.4.1) ---------------------------


def test_default_removal_matches_the_spec_example() -> None:
    """Spec 8.4.1's own example: an empty non-required repeated field goes;
    explicit optional falses, REQUIRED "" and REQUIRED [] stay."""
    fragment = {
        "name": "Example Agent",
        "description": "",
        "capabilities": {"streaming": False, "pushNotifications": False, "extensions": []},
        "skills": [],
    }
    assert signing_payload(fragment).decode() == (
        '{"capabilities":{"pushNotifications":false,"streaming":false},'
        '"description":"","name":"Example Agent","skills":[]}'
    )


def test_default_removal_by_field_presence() -> None:
    card = {
        "name": "a",
        "supportedInterfaces": [
            {"url": "https://x/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0", "tenant": ""}
        ],
        "documentationUrl": "",  # optional: explicitly set, so kept
        "securitySchemes": {},  # plain map at its default
        "skills": [{"id": "s", "name": "s", "description": "", "tags": ["t"], "examples": []}],
        "iconUrl": None,  # null is absence
        "futureField": 0,  # unknown to the schema: kept as sent
    }
    assert json.loads(signing_payload(card)) == {
        "name": "a",
        "supportedInterfaces": [
            {"url": "https://x/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "documentationUrl": "",
        "skills": [{"id": "s", "name": "s", "description": "", "tags": ["t"]}],
        "futureField": 0,
    }


def test_signatures_are_not_signed_content() -> None:
    card = {"name": "a", "description": "d"}
    with_sigs = {**card, "signatures": [{"protected": "x", "signature": "y"}]}
    assert signing_payload(with_sigs) == signing_payload(card)


# ---- signing and verification (spec 8.4.2, 8.4.3) ----------------------------------


def _card() -> AgentCardV1:
    return persona_card(
        name="echo", persona="echo", legacy={"description": "Echoes", "version": "0.1.0"},
        base_url="https://cabal.test",
    )


def test_signed_card_verifies_and_uses_a_jose_header() -> None:
    key = _key("k1")
    card = signed_card(_card(), key)
    assert verify_card(card, _key_set(key)) == "k1"
    (sig,) = card["signatures"]
    header = json.loads(base64.urlsafe_b64decode(sig["protected"] + "=" * (-len(sig["protected"]) % 4)))
    assert header == {"alg": "RS256", "typ": "JOSE", "kid": "k1"}


def test_a_reconstructed_card_still_verifies() -> None:
    """A receiver that fills defaults back in, or reorders keys, gets the
    same signed content (the point of 8.4.1 rule 1)."""
    key = _key("k1")
    card = signed_card(_card(), key)
    rebuilt = json.loads(json.dumps(card, sort_keys=True))
    rebuilt["supportedInterfaces"][0]["tenant"] = ""
    rebuilt["capabilities"]["extensions"] = []
    rebuilt["securitySchemes"] = {}
    assert verify_card(rebuilt, _key_set(key)) == "k1"


def test_a_changed_card_does_not_verify() -> None:
    key = _key("k1")
    card = signed_card(_card(), key)
    tampered = copy.deepcopy(card)
    tampered["supportedInterfaces"][0]["url"] = "https://evil.test/agents/echo/a2a"
    with pytest.raises(CardSignatureError):
        verify_card(tampered, _key_set(key))


def test_an_unknown_key_does_not_verify() -> None:
    card = signed_card(_card(), _key("k-gone"))
    with pytest.raises(CardSignatureError, match="unknown key"):
        verify_card(card, _key_set(_key("k-other")))


def test_a_signature_by_the_wrong_key_with_a_known_kid_does_not_verify() -> None:
    impostor = _key("k1")
    real = _key("k1")
    card = signed_card(_card(), impostor)
    with pytest.raises(CardSignatureError):
        verify_card(card, _key_set(real))


def test_only_rs256_is_accepted() -> None:
    key = _key("k1")
    card = signed_card(_card(), key)
    header = {"alg": "HS256", "typ": "JOSE", "kid": "k1"}
    card["signatures"][0]["protected"] = (
        base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    )
    with pytest.raises(CardSignatureError, match="HS256"):
        verify_card(card, _key_set(key))


def test_a_card_without_signatures_does_not_verify() -> None:
    with pytest.raises(CardSignatureError, match="no signatures"):
        verify_card(_card().to_wire(), _key_set(_key("k1")))


def test_any_verifying_signature_is_enough() -> None:
    """Key rotation: several signatures, the first by a key no longer held."""
    old, new = _key("k-old"), _key("k-new")
    wire = _card().to_wire()
    card = {**wire, "signatures": [sign_card(wire, old).to_wire(), sign_card(wire, new).to_wire()]}
    assert verify_card(card, _key_set(new)) == "k-new"


# ---- the persona card --------------------------------------------------------------


def test_persona_card_shape() -> None:
    card = persona_card(
        name="mason-primus",
        persona="mason",
        legacy={
            "description": "Plans and delegates",
            "version": "0.1.0",
            "skills": [{"id": "mason-primus", "name": "mason-primus"}],
        },
        base_url="https://cabal.test",
    ).to_wire()
    AgentCardV1.model_validate(card)
    assert card["supportedInterfaces"] == [
        {"url": "https://cabal.test/agents/mason-primus/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False, "extendedAgentCard": False}
    assert card["defaultInputModes"] == ["text/plain", "application/json"]
    assert card["skills"] == [
        {"id": "mason-primus", "name": "mason-primus", "description": "Plans and delegates", "tags": ["mason"]}
    ]
    assert "schemaVersion" not in card and "url" not in card


# ---- the public URL ----------------------------------------------------------------


def test_resolve_public_url(monkeypatch) -> None:
    monkeypatch.delenv("CABAL_PUBLIC_URL", raising=False)
    identity = IdentityConfig(cabal=CabalConfig(public_url="https://from-config.test/"))
    assert resolve_public_url(None) is None
    assert resolve_public_url(identity) == "https://from-config.test"
    monkeypatch.setenv("CABAL_PUBLIC_URL", "http://localhost:7400")
    assert resolve_public_url(identity) == "http://localhost:7400"
    for bad in ("cabal.test", "ftp://cabal.test", "https://cabal.test/?x=1"):
        monkeypatch.setenv("CABAL_PUBLIC_URL", bad)
        with pytest.raises(ValueError):
            resolve_public_url(identity)


# ---- the route ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_route_serves_a_signed_v1_card(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setenv("CABAL_PUBLIC_URL", "https://cabal.test/")
    r = await client.get("/agents/echo/.well-known/agent-card.json")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    card = r.json()
    AgentCardV1.model_validate(card)
    assert card["supportedInterfaces"][0]["url"] == "https://cabal.test/agents/echo/a2a"
    assert verify_card(card, app.state.jwks_provider())


@pytest.mark.asyncio
async def test_card_route_refuses_without_a_public_url(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.delenv("CABAL_PUBLIC_URL", raising=False)
    r = await client.get("/agents/echo/.well-known/agent-card.json")
    assert r.status_code == 503
    assert "public_url" in r.text
    monkeypatch.setenv("CABAL_PUBLIC_URL", "ftp://cabal.test")
    assert (await client.get("/agents/echo/.well-known/agent-card.json")).status_code == 503


@pytest.mark.asyncio
async def test_card_route_has_no_card_for_an_unknown_agent_or_a_federated_peer(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setenv("CABAL_PUBLIC_URL", "https://cabal.test")
    assert (await client.get("/agents/ghostly/.well-known/agent-card.json")).status_code == 404
    async with app.state.sessionmaker() as s:
        s.add(Persona(name="peer", config_json={}))
        await s.flush()
        await bulletin_store.register_federated(
            s,
            name="peer",
            persona_name="peer",
            endpoint="https://peer.example",
            federation_audience="peer@remote",
            agent_card_json={"name": "peer"},
            agent_card_kid="kid-peer",
        )
        await s.commit()
    assert (await client.get("/agents/peer/.well-known/agent-card.json")).status_code == 404


@pytest.mark.asyncio
async def test_legacy_card_route_is_unchanged(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv("CABAL_PUBLIC_URL", "https://cabal.test")
    r = await client.get("/agents/echo/.well-known/agent-card")
    assert r.status_code == 200
    assert "signatures" not in r.json()


@pytest.mark.asyncio
async def test_directory_lists_the_v1_card_url(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    from sqlalchemy import select

    async with app.state.sessionmaker() as s:
        code = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    token = (await client.post("/auth/bootstrap", json={"code": code.code})).json()["access_token"]
    monkeypatch.setenv("CABAL_PUBLIC_URL", "https://cabal.test")
    r = await client.get("/directory", headers={"Authorization": f"Bearer {token}"})
    echo = next(a for a in r.json()["agents"] if a["name"] == "echo")
    assert echo["agent_card_url"] == "https://cabal.test/agents/echo/.well-known/agent-card.json"
