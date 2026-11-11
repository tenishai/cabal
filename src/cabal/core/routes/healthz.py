"""Liveness probe."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from cabal import __version__

router = APIRouter()


@router.get("/healthz", tags=["meta"])
async def healthz(request: Request) -> dict[str, Any]:
    # Still 200 while draining (issue 405), so a supervisor doesn't restart
    # it; `draining` lets `ergon status` say so.
    body: dict[str, Any] = {"status": "ok", "version": __version__}
    dispatch_router = getattr(request.app.state, "dispatch_router", None)
    if dispatch_router is not None:
        body["draining"] = dispatch_router.running.draining
    return body
