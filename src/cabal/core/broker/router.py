"""Dispatch router — the keystone of Option C.

§6.12 invariants enforced here:

    1. AgentCard signature verification at registration time, cached.
    2. Caller JWT verification on EVERY dispatch, even in-process.
    3. Broker bookkeeping module on EVERY dispatch (audit + events).
    4. Trace context (cabal_trace_id, cabal_span_id) propagated via
       task.metadata, fresh-generated when absent.

The router is the single place these invariants live; tests assert each
one independently and assert them identically across solo + scale modes.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog
from joserfc.jwk import KeySet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.agents.base import LocalHandler
from cabal.core.a2a.types import Task
from cabal.core.auth.jwt import JWTError, verify
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.bulletin import store as bulletin_store
from cabal.core.bulletin.store import AgentRow
from cabal.core.db import open_session

log = structlog.get_logger(__name__)


class DispatchError(RuntimeError):
    """Routing-layer failures: missing target, JWT invalid, transport error."""


@dataclass(slots=True)
class DispatchContext:
    """Resolved + verified context for a single dispatch."""

    target: AgentRow
    caller_agent: str | None
    caller_persona: str | None


class JWKSProvider(Protocol):
    """Lookup current verification key set.

    Provided as a callable so rotation can replace the underlying KeySet
    atomically without the router holding a stale reference.
    """

    def __call__(self) -> KeySet: ...


def _ensure_trace(task: Task) -> Task:
    """Mutate task.metadata to ensure cabal_trace_id + cabal_span_id are set."""
    if not task.metadata.get("cabal_trace_id"):
        task.metadata["cabal_trace_id"] = secrets.token_hex(16)
    # A fresh span per hop, always.
    task.metadata["cabal_span_id"] = secrets.token_hex(8)
    return task


class DispatchRouter:
    """Routes Tasks to local handlers (solo mode) or HTTP peers (scale mode).

    Caller wires in:
        sessionmaker: for bulletin lookup
        bookkeeper: invariant 3
        jwks_provider: callable returning current KeySet (invariant 2)
        http_client: optional override for remote dispatch (tests)
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bookkeeper: Bookkeeper,
        jwks_provider: JWKSProvider,
        http_client: httpx.AsyncClient | None = None,
        a2a_path: str = "/a2a/tasks",
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bookkeeper = bookkeeper
        self._jwks_provider = jwks_provider
        self._http_client = http_client
        self._local: dict[str, LocalHandler] = {}
        self._a2a_path = a2a_path

    # --- registration -----------------------------------------------------

    def register_local(self, handler: LocalHandler) -> None:
        """Wire a LocalHandler into the in-process registry."""
        self._local[handler.name] = handler
        log.info("router.register_local", agent=handler.name)

    def has_local(self, name: str) -> bool:
        return name in self._local

    def unregister_local(self, name: str) -> None:
        self._local.pop(name, None)

    # --- dispatch ---------------------------------------------------------

    async def dispatch(
        self,
        *,
        target_agent: str,
        target_generation: int | None,
        task: Task,
        caller_jwt: str,
    ) -> Task:
        """Send `task` to `target_agent`.

        `target_generation` of None means "the active generation"; an explicit
        integer routes to that exact generation (used by subordinate returns).
        """
        ctx = await self._prepare(target_agent, target_generation, caller_jwt)
        task = _ensure_trace(task)
        await self._bookkeeper.on_dispatch_start(
            task, ctx.target, ctx.caller_agent
        )

        try:
            if ctx.target.name in self._local:
                response = await self._dispatch_local(self._local[ctx.target.name], task)
            else:
                response = await self._dispatch_remote(ctx.target, task, caller_jwt)
        except Exception as exc:
            await self._bookkeeper.on_dispatch_failure(task, exc)
            raise

        await self._bookkeeper.on_dispatch_success(task, response)
        return response

    # --- internal pieces --------------------------------------------------

    async def _prepare(
        self,
        target_agent: str,
        target_generation: int | None,
        caller_jwt: str,
    ) -> DispatchContext:
        async with open_session(self._sessionmaker) as session:
            if target_generation is None:
                row = await bulletin_store.get_active(session, target_agent)
            else:
                row = await bulletin_store.get(
                    session, target_agent, target_generation
                )
        if row is None:
            raise DispatchError(
                f"no agent registered: {target_agent!r}"
                + (
                    f" generation={target_generation}"
                    if target_generation is not None
                    else ""
                )
            )

        # Invariant 2: caller-JWT verification, even for in-process.
        try:
            payload = verify(
                caller_jwt,
                key_set=self._jwks_provider(),
                expected_audience=target_agent,
            )
        except JWTError as exc:
            raise DispatchError(f"caller JWT rejected: {exc}") from exc

        return DispatchContext(
            target=row,
            caller_agent=payload.get("sub"),
            caller_persona=payload.get("cabal_persona"),
        )

    async def _dispatch_local(self, handler: LocalHandler, task: Task) -> Task:
        return await handler.handle_task(task)

    async def _dispatch_remote(
        self, target: AgentRow, task: Task, caller_jwt: str
    ) -> Task:
        if self._http_client is None:
            raise DispatchError(
                "remote dispatch attempted without an http client wired in"
            )
        url = target.endpoint.rstrip("/") + self._a2a_path
        try:
            resp = await self._http_client.post(
                url,
                json=task.model_dump(by_alias=True, exclude_none=True),
                headers={"Authorization": f"Bearer {caller_jwt}"},
            )
        except httpx.HTTPError as exc:
            raise DispatchError(f"remote dispatch transport error: {exc}") from exc
        if resp.status_code >= 400:
            raise DispatchError(
                f"remote dispatch returned HTTP {resp.status_code}: {resp.text}"
            )
        return Task.model_validate(resp.json())
