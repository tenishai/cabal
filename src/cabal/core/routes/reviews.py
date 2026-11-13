"""Review runs over HTTP (cabal issue 399), operator only.

POST /reviews              queue a review of one PR head; returns the run at once
GET  /reviews              list runs, newest first (?state=, ?limit=)
GET  /reviews/{id}         one run and its steps
POST /reviews/{id}/cancel  cancel a run that hasn't ended

Queuing the same (repo, pr, sha) twice returns the first run with
`created: false`; a head is reviewed once. Nothing here dispatches a seat:
the engine that claims queued runs is a later step of issue 399.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cabal.core import reviews as store
from cabal.core.auth.jwt import extract_bearer
from cabal.core.db import open_session
from cabal.core.routes.admin import _require_operator_claims

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewRequest(BaseModel):
    repo: str
    pr: int
    sha: str
    seats: list[str] | None = None
    trigger: str = "cli"


class CancelRequest(BaseModel):
    reason: str = Field(default="canceled by the operator", max_length=500)


def _default_seats(request: Request) -> list[str] | None:
    """`[review] seats_default`, or None for the built-in default."""
    cfg = getattr(request.app.state, "review_config", None)
    return list(cfg.seats_default) if cfg is not None else None


@router.post("")
async def queue_review(
    request: Request,
    req: ReviewRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _require_operator_claims(request, extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        try:
            run, created = await store.create_run(
                session,
                repo=req.repo,
                pr=req.pr,
                sha=req.sha,
                seats=req.seats or _default_seats(request),
                requested_by=claims.get("sub"),
                trigger=req.trigger,
            )
        except store.InvalidRun as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        body = store.to_dict(run)
    return {"created": created, "run": body}


@router.get("")
async def list_reviews(
    request: Request,
    state: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator_claims(request, extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        try:
            runs = await store.list_runs(session, state=state, limit=limit)
        except store.InvalidRun as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"runs": [store.to_dict(r) for r in runs]}


@router.get("/{run_id}")
async def show_review(
    request: Request,
    run_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator_claims(request, extract_bearer(authorization))
    async with open_session(request.app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no review run {run_id}")
        return {"run": store.to_dict(run)}


@router.post("/{run_id}/cancel")
async def cancel_review(
    request: Request,
    run_id: str,
    req: CancelRequest | None = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_operator_claims(request, extract_bearer(authorization))
    reason = req.reason if req is not None else CancelRequest().reason
    async with open_session(request.app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no review run {run_id}")
        try:
            running = await store.cancel_run(session, run, reason=reason)
        except store.InvalidRun as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except store.StaleRun as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    # The seat that was running is told to stop, through the abort graph.
    coordinator = request.app.state.dispatch_router.abort_coordinator
    aborted = sum(int(coordinator.abort(task_id, reason="review run canceled")) for task_id in running)
    async with open_session(request.app.state.sessionmaker) as session:
        fresh = await store.get_run(session, run_id)
        assert fresh is not None
        return {"run": store.to_dict(fresh), "aborted": aborted}
