"""Liveness probe."""

from __future__ import annotations

from fastapi import APIRouter

from cabal import __version__

router = APIRouter()


@router.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
