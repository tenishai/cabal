"""The token meter's route (cabal issue 394, part 2): `/meter/<key>/<path>`.

A metered agent's model base URL is `<cabal>/meter/<key>`, so its client sends
`POST /meter/<key>/v1/messages` (Anthropic) or `/meter/<key>/chat/completions`
(OpenAI-compatible). The key is the only credential this route checks; the
caller's own `Authorization` goes to the upstream untouched. An unknown key, or
a cabal with the meter off, gets a 404 that says nothing about which.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

router = APIRouter()

_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@router.api_route("/meter/{key}/{path:path}", methods=_METHODS, include_in_schema=False)
async def meter_call(key: str, path: str, request: Request) -> Response:
    meter = getattr(request.app.state, "meter", None)
    found = meter.agent_for(key) if meter is not None else None
    if meter is None or found is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    agent, upstream = found
    return await meter.forward(agent, upstream, path, request)
