"""A2A Task endpoint.

POST /a2a/tasks — accept a Task, hand it to the dispatch router, return
the response Task.

Phase 1 surface uses a thin wrapper around the A2A Task schema and routes
based on a header-supplied target agent name. A2A v1.0 also defines a
JSON-RPC envelope (message/send, tasks/get) — that's Phase 2 once we
need streaming and stateful Task resumption from the wire.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from cabal.core.a2a.types import Task
from cabal.core.broker.router import DispatchError

router = APIRouter(prefix="/a2a", tags=["a2a"])


class TaskRequest(BaseModel):
    target: str
    target_generation: int | None = None
    task: Task


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    return authorization.split(" ", 1)[1].strip()


@router.post("/tasks", response_model=Task)
async def submit_task(
    req: TaskRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Task:
    token = _extract_bearer(authorization)
    router_ = request.app.state.dispatch_router
    try:
        return await router_.dispatch(
            target_agent=req.target,
            target_generation=req.target_generation,
            task=req.task,
            caller_jwt=token,
        )
    except DispatchError as exc:
        msg = str(exc)
        if "caller JWT" in msg:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=msg
            ) from exc
        if "no agent registered" in msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=msg
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=msg
        ) from exc
