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

from fastapi import Request

from cabal.core import dispatch_pairs
from cabal.core.auth.jwt import JWTError, mint, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.auth.revocations import refusal
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session


@dataclass(frozen=True, slots=True)
class DispatchRefusal:
    """Why a token may not dispatch.

    `kind` is what a route needs to choose its answer: `unauthenticated` (no
    valid, unrevoked token), `forbidden` (a valid token the policy refuses),
    or `internal` (a server-side fault). `legacy_forbidden` keeps the legacy
    route's historical split: it answers a scope refusal as unauthorized and
    only a dispatch-pairs refusal as forbidden.
    """

    kind: Literal["unauthenticated", "forbidden", "internal"]
    message: str
    legacy_forbidden: bool = False


def verify_caller(request: Request, raw_token: str) -> dict[str, Any] | DispatchRefusal:
    """The token's claims, or a refusal if it is invalid or revoked."""
    try:
        claims = verify(raw_token, key_set=request.app.state.jwks_provider(), expected_audience=None)
    except JWTError as exc:
        return DispatchRefusal("unauthenticated", str(exc))
    if (why := refusal(getattr(request.app.state, "revocations", None), claims)) is not None:
        return DispatchRefusal("unauthenticated", why)
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
    """
    claims = verify_caller(request, raw_token)
    if isinstance(claims, DispatchRefusal):
        return claims

    async with open_session(request.app.state.sessionmaker) as session:
        audience = await dispatch_audience(session, target)
        if audience is None:
            return DispatchRefusal(
                "internal", f"federated target {target!r} has no federation_audience"
            )
        if claims.get("aud") == audience:
            return raw_token

        scope = claims.get("cabal_scope") or []
        if not ({"operator", "colleague"} & set(scope)):
            return DispatchRefusal(
                "forbidden",
                f"token aud={claims.get('aud')!r} cannot dispatch to {target!r} "
                "(requires operator or colleague scope)",
            )

        sub = claims.get("sub") or ""
        # A token with no sub failed to identify its bearer (cabal#184). Refuse
        # to re-mint it rather than default the principal to "operator".
        if not sub:
            return DispatchRefusal(
                "unauthenticated", "token carries no sub; refusing to re-mint a dispatch token"
            )

        # Scope says who may ask for a re-mint; the pairs table says who may
        # call whom. An empty table is open (cabal#127).
        if "operator" not in scope and not await dispatch_pairs.is_allowed(
            session, caller=sub, target=target
        ):
            return DispatchRefusal(
                "forbidden",
                f"{sub} is not a colleague of {target} yet. Ask in the room.",
                legacy_forbidden=True,
            )
        stored = await load_active_signing_key(session, master_key=request.app.state.master_key)
    if stored is None:
        return DispatchRefusal("internal", "no active signing key")
    return mint(
        signing_key=stored,
        sub=sub,
        aud=audience,
        persona=claims.get("cabal_persona"),
        scope=["dispatch"],
    )
