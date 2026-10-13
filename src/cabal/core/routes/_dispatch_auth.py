"""Who may dispatch to an agent: one policy for the network doors (#267, #234).

The REST route (`routes/tasks.py`), the legacy JSON-RPC route
(`routes/a2a_jsonrpc.py`) and the A2A v1.0 route (`routes/a2a_v1.py`) all
dispatch to a persona on behalf of a bearer token. They must agree on who may
do that, so the decision lives here once and each route only translates a
refusal into its own wire form.

The TUI's in-process client (`tui/client/inprocess.py`) keeps its own policy,
where only operator scope may re-mint, but takes the token's audience from
`dispatch_audience` like the doors do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog
from fastapi import Request

from cabal.core import dispatch_pairs
from cabal.core.auth.jwt import JWTError, mint, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.auth.revocations import refusal
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Event

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchRefusal:
    """Why a token may not dispatch.

    `kind` is what a route needs to choose its answer: `unauthenticated` (no
    valid, unrevoked token), `forbidden` (a valid token the policy refuses),
    or `internal` (a server-side fault). `legacy_forbidden` keeps the legacy
    route's historical split: it answers a scope refusal as unauthorized and
    only a dispatch-pairs refusal as forbidden.

    `check` names which check refused, for the operator's log and audit row
    (#315): token, revoked, audience, scope, sub, pair or signing_key.

    `remedy` is a machine-readable code for the fix, when there is one a
    person can act on, so a runtime can pass it on without parsing prose
    (#329). Only the pair check sets one today: `colleague_invite`. Every
    route sends it next to the message: REST and v1.0 through `body_fields`,
    legacy JSON-RPC (send and stream) as the error's `data`.
    """

    kind: Literal["unauthenticated", "forbidden", "internal"]
    message: str
    legacy_forbidden: bool = False
    check: str = ""
    remedy: str = ""

    def body_fields(self) -> dict[str, str]:
        """`{"detail": message}` plus `remedy` when set: the body the REST and
        A2A v1.0 routes send. `detail` stays a string: daedalus 0.5.0's
        `_refusal_detail` passes the body's `detail` through `str()` into the
        text the model sees."""
        fields = {"detail": self.message}
        if self.remedy:
            fields["remedy"] = self.remedy
        return fields


def verify_caller(request: Request, raw_token: str) -> dict[str, Any] | DispatchRefusal:
    """The token's claims, or a refusal if it is invalid or revoked."""
    try:
        claims = verify(raw_token, key_set=request.app.state.jwks_provider(), expected_audience=None)
    except JWTError as exc:
        return DispatchRefusal("unauthenticated", str(exc), check="token")
    if (why := refusal(getattr(request.app.state, "revocations", None), claims)) is not None:
        return DispatchRefusal("unauthenticated", why, check="revoked")
    return claims


async def dispatch_audience(session: Any, target: str) -> str | None:
    """The `aud` a dispatch token for `target` must carry.

    A `federated` target's token is forwarded to the sovereign peer, so its
    audience is that peer's service id, not the bare agent name (cabal#45).
    None means a federated target with no `federation_audience`, which is a
    registration fault.
    """
    row = await bulletin_store.get_active(session, target)
    if row is not None and row.kind == "federated":
        return row.federation_audience or None
    return target


async def dispatch_token(request: Request, raw_token: str, target: str) -> str | DispatchRefusal:
    """A token that may dispatch to `target`, or why there is none.

    A token already aimed at the target's audience is used as it is.
    Otherwise the caller needs operator or colleague scope, a `sub`, and
    (unless operator) an allowed caller-to-target pair, and gets a fresh
    dispatch token minted for that audience.

    Every refusal is recorded here, once, for all three doors (#315): a
    warning log line, and for a caller whose token verified, a
    `dispatch_refused` row in the events table. Before this, the caller saw
    the reason in its error response (401, 403 or 500) and the server kept
    nothing.
    """
    claims = verify_caller(request, raw_token)
    if isinstance(claims, DispatchRefusal):
        # An unverified caller gets a log line, never a database row: anyone
        # who can reach the port can send a bad token.
        await _record_refusal(request, claims, caller=None, target=target, audit=False)
        return claims
    result = await _dispatch_token(request, raw_token, target, claims)
    if isinstance(result, DispatchRefusal):
        await _record_refusal(request, result, caller=claims.get("sub") or None, target=target)
    return result


#: Longest caller, target or reason recorded. `target` comes from the request
#: body and is otherwise unbounded; agent names are at most 64 characters.
_RECORD_MAX = 128


def _bounded(value: str | None) -> str | None:
    if value is None or len(value) <= _RECORD_MAX:
        return value
    return value[:_RECORD_MAX] + "...[truncated]"


async def _record_refusal(
    request: Request,
    refused: DispatchRefusal,
    *,
    caller: str | None,
    target: str,
    audit: bool = True,
) -> None:
    """Log one refusal and, when `audit`, write its events row. Never the
    token. caller, target and reason are capped at _RECORD_MAX; kind and
    check are fixed literals set in this module. An audit write that fails is
    logged and swallowed: the caller still gets its refusal."""
    fields = {
        "caller": _bounded(caller),
        "target": _bounded(target),
        "kind": refused.kind,
        "check": refused.check,
        "reason": _bounded(refused.message),
    }
    log.warning("dispatch.refused", **fields)
    if not audit:
        return
    try:
        async with open_session(request.app.state.sessionmaker) as session:
            session.add(Event(task_id=None, event_type="dispatch_refused", payload_json=fields))
    except Exception as exc:  # noqa: BLE001 -- the refusal must still go out
        log.warning("dispatch.refused_audit_failed", error=str(exc), **fields)


async def _dispatch_token(
    request: Request, raw_token: str, target: str, claims: dict[str, Any]
) -> str | DispatchRefusal:

    async with open_session(request.app.state.sessionmaker) as session:
        audience = await dispatch_audience(session, target)
        if audience is None:
            return DispatchRefusal(
                "internal",
                f"federated target {target!r} has no federation_audience",
                check="audience",
            )
        if claims.get("aud") == audience:
            return raw_token

        scope = claims.get("cabal_scope") or []
        if not ({"operator", "colleague"} & set(scope)):
            return DispatchRefusal(
                "forbidden",
                f"token aud={claims.get('aud')!r} cannot dispatch to {target!r} "
                "(requires operator or colleague scope)",
                check="scope",
            )

        sub = claims.get("sub") or ""
        # A token with no sub failed to identify its bearer (cabal#184). Refuse
        # to re-mint it rather than default the principal to "operator".
        if not sub:
            return DispatchRefusal(
                "unauthenticated",
                "token carries no sub; refusing to re-mint a dispatch token",
                check="sub",
            )

        # Scope says who may ask for a re-mint; the pairs table says who may
        # call whom. An empty table is open (cabal#127).
        if "operator" not in scope and not await dispatch_pairs.is_allowed(
            session, caller=sub, target=target
        ):
            return DispatchRefusal(
                "forbidden",
                f"{sub} is not a colleague of {target} yet: {sub}'s owner can invite "
                f"{target} as a colleague, and {target}'s owner accepts.",
                legacy_forbidden=True,
                check="pair",
                remedy="colleague_invite",
            )
        stored = await load_active_signing_key(session, master_key=request.app.state.master_key)
    if stored is None:
        return DispatchRefusal("internal", "no active signing key", check="signing_key")
    return mint(
        signing_key=stored,
        sub=sub,
        aud=audience,
        persona=claims.get("cabal_persona"),
        scope=["dispatch"],
    )
