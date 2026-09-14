"""GET /directory — the colleague-readable agent roster.

The `colleague` scope (minted by a separate room service, `sub` shaped
like "agent:<name>") grants exactly two things: this read, and dispatch
re-mint (see `tasks.py:_resolve_dispatch_token`). Nothing else — a
colleague token must still get 403 on every /admin route.

Serialization reads the bulletin board's `agent_card_json` tolerantly: it
may be a bare AgentCard dict, or the `{"jws": ..., "card": {...}}` shape
`sign_card` produces. Missing metadata fields default to empty rather
than raising, since not every registered agent (the built-in echo, for
one) carries a `metadata` block.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from cabal.core.auth.jwt import extract_bearer
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.routes.admin import _AUD_OPERATOR, _require_scope

router = APIRouter(tags=["directory"])


@router.get("/directory")
async def list_directory(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_scope(
        request,
        extract_bearer(authorization),
        audiences={_AUD_OPERATOR},
        any_of={"operator", "colleague"},
    )
    async with open_session(request.app.state.sessionmaker) as session:
        rows = await bulletin_store.list_all(session)

    agents = []
    for row in rows:
        # cabal#142 presence rows share this table and carry status="active",
        # so filtering on status alone hands a colleague the operator's live
        # Claude Code session names. A session is not dispatchable and is not
        # a colleague's business; skip it before the status check.
        if row.kind == "session":
            continue
        if row.status != "active":
            continue
        card = (row.agent_card_json or {}).get("card", row.agent_card_json or {})
        meta = card.get("metadata") or {}
        agents.append(
            {
                "name": row.name,
                "team": meta.get("team", ""),
                "purpose_tags": meta.get("purpose_tags", []),
                "capability": meta.get("capability", ""),
                "description": card.get("description", ""),
            }
        )
    return {"agents": agents}
