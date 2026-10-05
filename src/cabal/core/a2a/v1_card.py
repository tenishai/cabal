"""A2A v1.0 AgentCards: the model, canonical form and embedded signatures (#270).

A v1.0 card is a JSON object with its signatures embedded in a `signatures`
array (spec 4.4.1, 4.4.7). Each signature is a JWS whose payload is the card
itself in canonical form (spec 8.4.1):

1. Fields at their default value are removed, by proto field presence
   (spec 5.7): a REQUIRED field always stays, an `optional` field stays when
   it was set, and any other field is removed when it holds its default
   (`""`, `0`, `false`, an empty list or map). See `_FIELDS`.
2. The `signatures` field is removed.
3. The rest is serialized by RFC 8785 (JCS).

cabal's legacy cards (`a2a/card.py`) are a different format: a compact JWS
next to the card, over a minimal JCS. They are unchanged.
"""

from __future__ import annotations

import base64
import json
import math
from decimal import Decimal
from typing import Any, Literal

from joserfc import jws
from joserfc.jwk import KeySet, RSAKey
from pydantic import Field

from cabal.core.a2a.v1 import _V1, PartV1
from cabal.core.auth.keys import StoredKey

SIGNING_ALG = "RS256"

# ---- the model -----------------------------------------------------------------


class AgentInterfaceV1(_V1):
    url: str
    protocol_binding: str
    tenant: str | None = None
    protocol_version: str


class AgentExtensionV1(_V1):
    uri: str | None = None
    description: str | None = None
    required: bool | None = None
    params: dict[str, Any] | None = None


class AgentCapabilitiesV1(_V1):
    streaming: bool | None = None
    push_notifications: bool | None = None
    extensions: list[AgentExtensionV1] | None = None
    extended_agent_card: bool | None = None


class AgentProviderV1(_V1):
    url: str
    organization: str


class AgentSkillV1(_V1):
    id: str
    name: str
    description: str
    tags: list[str] = Field(min_length=1)
    examples: list[str] | None = None
    input_modes: list[str] | None = None
    output_modes: list[str] | None = None


class HTTPAuthSecuritySchemeV1(_V1):
    description: str | None = None
    scheme: str
    bearer_format: str | None = None


class SecuritySchemeV1(_V1):
    """A proto oneof: exactly one scheme is set. cabal declares only HTTP
    auth; the other kinds are kept as raw objects for reading foreign cards."""

    api_key_security_scheme: dict[str, Any] | None = None
    http_auth_security_scheme: HTTPAuthSecuritySchemeV1 | None = None
    oauth2_security_scheme: dict[str, Any] | None = None
    open_id_connect_security_scheme: dict[str, Any] | None = None
    mtls_security_scheme: dict[str, Any] | None = None


class StringListV1(_V1):
    values: list[str] | None = Field(default=None, alias="list")


class SecurityRequirementV1(_V1):
    # Scheme name -> required scopes (empty for a bearer token).
    schemes: dict[str, StringListV1] | None = None


class AgentCardSignatureV1(_V1):
    protected: str
    signature: str
    header: dict[str, Any] | None = None


class AgentCardV1(_V1):
    """A v1.0 AgentCard. REQUIRED arrays must hold at least one element
    (spec 5.7); `skills` is REQUIRED but the spec's own canonicalization
    example keeps it empty, so only the interface and mode lists are bound."""

    name: str
    description: str
    supported_interfaces: list[AgentInterfaceV1] = Field(min_length=1)
    provider: AgentProviderV1 | None = None
    version: str
    documentation_url: str | None = None
    capabilities: AgentCapabilitiesV1
    security_schemes: dict[str, SecuritySchemeV1] | None = None
    security_requirements: list[SecurityRequirementV1] | None = None
    default_input_modes: list[str] = Field(min_length=1)
    default_output_modes: list[str] = Field(min_length=1)
    skills: list[AgentSkillV1]
    signatures: list[AgentCardSignatureV1] | None = None
    icon_url: str | None = None


# ---- default-value removal (spec 5.7, 8.4.1 rule 1) -----------------------------

# R: REQUIRED, always kept. O: `optional` (or a message field, which always
# has presence), kept when present. P: plain proto3 field, removed at its
# default. A tuple names the message type of the field's value; "map:T" is a
# map whose values are T messages.
_Kind = Literal["R", "O", "P"]
_FieldSpec = _Kind | tuple[_Kind, str]
_FIELDS: dict[str, dict[str, _FieldSpec]] = {
    "AgentCard": {
        "name": "R",
        "description": "R",
        "supportedInterfaces": ("R", "AgentInterface"),
        "provider": ("O", "AgentProvider"),
        "version": "R",
        "documentationUrl": "O",
        "capabilities": ("R", "AgentCapabilities"),
        "securitySchemes": ("P", "map:SecurityScheme"),
        "securityRequirements": ("P", "SecurityRequirement"),
        "defaultInputModes": "R",
        "defaultOutputModes": "R",
        "skills": ("R", "AgentSkill"),
        "iconUrl": "O",
    },
    "AgentInterface": {
        "url": "R",
        "protocolBinding": "R",
        "tenant": "P",
        "protocolVersion": "R",
    },
    "AgentCapabilities": {
        "streaming": "O",
        "pushNotifications": "O",
        "extensions": ("P", "AgentExtension"),
        "extendedAgentCard": "O",
    },
    "AgentExtension": {
        "uri": "P",
        "description": "P",
        "required": "P",
        "params": "O",
    },
    "AgentProvider": {"url": "R", "organization": "R"},
    "AgentSkill": {
        "id": "R",
        "name": "R",
        "description": "R",
        "tags": "R",
        "examples": "P",
        "inputModes": "P",
        "outputModes": "P",
        "securityRequirements": ("P", "SecurityRequirement"),
    },
    "SecurityScheme": {
        "apiKeySecurityScheme": "O",
        "httpAuthSecurityScheme": ("O", "HTTPAuthSecurityScheme"),
        "oauth2SecurityScheme": "O",
        "openIdConnectSecurityScheme": "O",
        "mtlsSecurityScheme": "O",
    },
    "HTTPAuthSecurityScheme": {"description": "P", "scheme": "R", "bearerFormat": "P"},
    "SecurityRequirement": {"schemes": ("P", "map:StringList")},
    "StringList": {"list": "P"},
}


def _is_default(value: Any) -> bool:
    return value is None or value == "" or value is False or value == [] or value == {} or (
        isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0
    )


def _strip(value: Any, message: str) -> Any:
    fields = _FIELDS[message]
    out: dict[str, Any] = {}
    for key, item in value.items():
        spec = fields.get(key)
        if spec is None:
            # Not in the schema: kept as sent, since a verifier cannot know
            # its default and dropping it would let it change unsigned.
            out[key] = item
            continue
        kind, child = spec if isinstance(spec, tuple) else (spec, None)
        if item is None:
            continue  # JSON null is absence in ProtoJSON
        if kind == "P" and _is_default(item):
            continue
        if child is not None and child.startswith("map:"):
            if isinstance(item, dict):
                value_type = child[len("map:") :]
                item = {k: _strip(v, value_type) if isinstance(v, dict) else v for k, v in item.items()}
        elif child is not None:
            if isinstance(item, list):
                item = [_strip(i, child) if isinstance(i, dict) else i for i in item]
            elif isinstance(item, dict):
                item = _strip(item, child)
        out[key] = item
    return out


def signing_payload(card: dict[str, Any]) -> bytes:
    """The bytes a card's signatures cover (spec 8.4.1): defaults removed,
    `signatures` removed, RFC 8785."""
    body = {k: v for k, v in card.items() if k != "signatures"}
    return jcs(_strip(body, "AgentCard"))


# ---- RFC 8785 ------------------------------------------------------------------


class CanonicalizationError(ValueError):
    """The value has no RFC 8785 form: NaN, an infinity, a lone surrogate,
    or a type JSON does not have."""


_ESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r", '"': '\\"', "\\": "\\\\"}


def _string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("a string holds a lone surrogate") from exc
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _number(x: float) -> str:
    """ECMAScript Number::toString (ECMA-262 7.1.12.1), which RFC 8785 3.2.2.3
    requires. Python's repr gives the shortest digits that round-trip, which
    is the digit string that algorithm chooses."""
    if math.isnan(x) or math.isinf(x):
        raise CanonicalizationError("NaN and infinities have no JSON form")
    if x == 0:
        return "0"  # covers -0
    sign = "-" if x < 0 else ""
    parts = Decimal(repr(abs(x))).as_tuple()
    digits = "".join(map(str, parts.digits)).rstrip("0")
    exp = int(parts.exponent) + len(parts.digits) - len(digits)
    k = len(digits)
    n = exp + k  # x = 0.digits * 10**n
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return sign + mantissa + "e" + ("+" if e > 0 else "-") + str(abs(e))


def _utf16_key(s: str) -> bytes:
    # Big-endian UTF-16 bytes compare in the same order as the code units.
    return s.encode("utf-16-be", "surrogatepass")


def _jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        try:
            return _number(float(value))
        except OverflowError as exc:
            raise CanonicalizationError("an integer is too large for a double") from exc
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(_string(k) + ":" + _jcs(value[k]) for k in keys) + "}"
    raise CanonicalizationError(f"{type(value).__name__} is not a JSON type")


def jcs(value: Any) -> bytes:
    """`value` serialized by RFC 8785, as UTF-8."""
    return _jcs(value).encode("utf-8")


# ---- signatures (spec 8.4.2, 8.4.3) -------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_card(card: dict[str, Any], signing_key: StoredKey) -> AgentCardSignatureV1:
    """A signature over `card` by `signing_key`: `alg`, `typ: JOSE` and `kid`
    in the protected header (spec 8.4.2)."""
    header = {"alg": SIGNING_ALG, "typ": "JOSE", "kid": signing_key.kid}
    compact = jws.serialize_compact(
        header, signing_payload(card), RSAKey.import_key(signing_key.private_key)
    )
    protected, _, signature = compact.split(".")
    return AgentCardSignatureV1(protected=protected, signature=signature)


def signed_card(card: AgentCardV1, signing_key: StoredKey) -> dict[str, Any]:
    """`card` as JSON with one signature by `signing_key` embedded."""
    wire = card.model_copy(update={"signatures": None}).to_wire()
    signature = sign_card(wire, signing_key)
    return {**wire, "signatures": [signature.to_wire()]}


class CardSignatureError(ValueError):
    """No signature on the card verifies against the given keys."""


def verify_card(card: dict[str, Any], key_set: KeySet) -> str:
    """Verify `card` per spec 8.4.3; return the `kid` of the first signature
    that verifies. Only RS256 is accepted, and a `kid` outside `key_set`
    (unknown, or a key no longer accepted) does not verify."""
    signatures = card.get("signatures") or []
    if not signatures:
        raise CardSignatureError("the card has no signatures")
    payload = _b64(signing_payload(card))
    reasons = []
    for sig in signatures:
        try:
            protected = sig["protected"]
            header = json.loads(base64.urlsafe_b64decode(protected + "=" * (-len(protected) % 4)))
            kid = header.get("kid")
            if header.get("alg") != SIGNING_ALG:
                raise CardSignatureError(f"alg {header.get('alg')!r} is not {SIGNING_ALG}")
            if not kid or all(k.kid != kid for k in key_set.keys):
                raise CardSignatureError(f"unknown key {kid!r}")
            jws.deserialize_compact(
                f"{protected}.{payload}.{sig['signature']}", key_set, algorithms=[SIGNING_ALG]
            )
            return str(kid)
        except Exception as exc:  # noqa: BLE001 - every failure means "try the next one"
            reasons.append(str(exc) or type(exc).__name__)
    raise CardSignatureError("no signature verified: " + "; ".join(reasons))


# ---- cabal's persona cards -----------------------------------------------------

# SendMessage takes text and data parts (`message_from_v1`); a reply carries
# text, and an artifact that is not in the A2A shape travels as a data part.
# The v1.0 methods that act on tasks (SendMessage, GetTask, ListTasks,
# CancelTask) verify the bearer token as a JWT this cabal issued. Outside
# agents get one from the operator (#271).
BEARER_SCHEME = "bearer"
_BEARER_DESCRIPTION = (
    "A JWT issued by this cabal. Outside agents get one from the cabal's operator."
)

INPUT_MODES = ("text/plain", "application/json")
OUTPUT_MODES = ("text/plain", "application/json")


def part_media_type(part: PartV1) -> str:
    """A v1.0 part's media type, lower-cased and without parameters: its
    `mediaType` when set, otherwise the type its kind implies (text is
    text/plain, data is application/json, raw and url are
    application/octet-stream)."""
    if part.media_type:
        return part.media_type.split(";", 1)[0].strip().lower()
    if part.text is not None:
        return "text/plain"
    if "data" in part.model_fields_set:
        return "application/json"
    return "application/octet-stream"


def persona_card(
    *, name: str, persona: str | None, legacy: dict[str, Any], base_url: str
) -> AgentCardV1:
    """The v1.0 card for the bulletin agent `name`, built from its legacy card.

    The one interface is the agent's own v1.0 endpoint under `base_url`. The
    capabilities are all off, matching the operations the endpoint declines
    (#267, #268). The card requires a cabal-issued bearer JWT (#271). A v1.0 skill needs a description and at least one tag; a
    legacy skill without them takes the card's description (or its name) and
    the persona's name.
    """
    description = str(legacy.get("description") or "")
    tag = persona or name
    skills = []
    for raw in legacy.get("skills") or [{"id": name, "name": name}]:
        skills.append(
            AgentSkillV1(
                id=str(raw.get("id") or name),
                name=str(raw.get("name") or name),
                description=str(raw.get("description") or description or name),
                tags=list(raw.get("tags") or [tag]),
            )
        )
    return AgentCardV1(
        security_schemes={
            BEARER_SCHEME: SecuritySchemeV1(
                http_auth_security_scheme=HTTPAuthSecuritySchemeV1(
                    description=_BEARER_DESCRIPTION, scheme="Bearer", bearer_format="JWT"
                )
            )
        },
        security_requirements=[SecurityRequirementV1(schemes={BEARER_SCHEME: StringListV1()})],
        name=name,
        description=description,
        supported_interfaces=[
            AgentInterfaceV1(
                url=f"{base_url}/agents/{name}/a2a",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        version=str(legacy.get("version") or ""),
        capabilities=AgentCapabilitiesV1(
            streaming=False, push_notifications=False, extended_agent_card=False
        ),
        default_input_modes=list(INPUT_MODES),
        default_output_modes=list(OUTPUT_MODES),
        skills=skills,
    )
