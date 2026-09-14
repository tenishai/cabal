"""A terminal verdict reaches the caller as a refusal, on BOTH non-streaming routes.

cabal#152 pre-pass, WRONG 1. `router.dispatch()` was given a TERMINATE branch that
raises `GuardrailBlocked`, and a comment beside it claimed the raise inherited the
FORBIDDEN reporting #153 built. It did not: that wiring lives only in
`_stream_task_events`, and `GuardrailBlocked` subclasses `DispatchError`, so both
callers of the non-streaming method caught the base class and answered "server
fault, retry" -- 502 on REST, -32603 on JSON-RPC. A retry-inviting code is the one
answer a terminal verdict must not give, since retrying is the amplification the
verdict exists to stop.

WHY THESE DRIVE THE REAL ROUTES rather than asserting the exception type. The arms
are ORDER-DEPENDENT -- `except GuardrailBlocked` must precede `except DispatchError`
or the subclass never matches -- and moving one below the other is a tidy that ruff
and mypy both wave through while silently restoring the old code. Nothing below the
route can see that. Same reasoning, and the same shape, as
test_streaming.py::test_a_blocked_stream_reports_forbidden_on_the_wire.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.a2a.jsonrpc import ERROR_FORBIDDEN, ERROR_INTERNAL
from cabal.core.models import BootstrapCode


async def _bootstrap_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(BootstrapCode).where(BootstrapCode.used_at.is_(None))
            )
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


class _TerminatingGuard:
    """TERMINATEs every dispatch. The reason text is asserted on the response."""

    async def check(self, target: Any, ctx: Any) -> Any:
        from cabal.core.guardrails import Verdict

        return Verdict.terminate(reason="edge not declared")


@pytest.mark.asyncio
async def test_a_terminated_dispatch_is_forbidden_on_the_jsonrpc_wire(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.dispatch_router._input_guardrails.append(_TerminatingGuard())
    token = await _bootstrap_token(app, client)

    r = await client.post(
        "/agents/echo",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("result") is None, "a terminated dispatch returned a Task"
    # THE ASSERTION THAT DIES IF THE ARMS ARE REORDERED.
    assert body["error"]["code"] == ERROR_FORBIDDEN, body
    assert body["error"]["code"] != ERROR_INTERNAL
    assert "edge not declared" in body["error"]["message"]


@pytest.mark.asyncio
async def test_a_terminated_dispatch_is_403_not_502_on_the_rest_route(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.dispatch_router._input_guardrails.append(_TerminatingGuard())
    token = await _bootstrap_token(app, client)

    r = await client.post(
        "/a2a/tasks",
        json={
            "target": "echo",
            "task": {
                "id": "t-152",
                "status": {
                    "state": "submitted",
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hi"}],
                    },
                },
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    # 502 says the upstream failed and the request was fine. It is also what a
    # client retries, which is the behaviour this verdict exists to end.
    assert r.status_code == 403, r.text
    assert "edge not declared" in r.json()["detail"]
