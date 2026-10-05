"""A reverse proxy that adds a bearer token to every request (#261, #271).

The A2A compatibility kit sends no credentials and has no option to, and
cabal refuses every task call without a cabal-issued token. #271 decided
that the kit gets through a proxy like this one in its CI job, so cabal's own
auth stays as it is. It forwards everything to $TCK_UPSTREAM (cabal) with
`Authorization: Bearer $TCK_TOKEN` and listens on $TCK_PROXY_PORT. cabal's
public URL is set to this proxy, so the cards it serves point the kit here.

The cards declare streaming off, so responses are buffered, not streamed.
"""

from __future__ import annotations

import os

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

UPSTREAM = os.environ.get("TCK_UPSTREAM", "http://127.0.0.1:7400")
TOKEN = os.environ.get("TCK_TOKEN", "")
# Connection-level headers a proxy does not forward (RFC 9110 7.6.1).
_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length"}

client = httpx.AsyncClient(base_url=UPSTREAM, timeout=60.0)


async def forward(request: Request) -> Response:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    headers["authorization"] = f"Bearer {TOKEN}"
    upstream = await client.request(
        request.method,
        request.url.path,
        params=request.query_params,
        content=await request.body(),
        headers=headers,
    )
    out = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP | {"content-encoding"}}
    return Response(upstream.content, status_code=upstream.status_code, headers=out)


app = Starlette(
    routes=[Route("/{path:path}", forward, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])]
)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("set TCK_TOKEN")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TCK_PROXY_PORT", "8080")))
