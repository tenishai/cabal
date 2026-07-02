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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog
from joserfc.jwk import KeySet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.agents.base import LocalHandler
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.abort import KIND_DISPATCH, AbortCoordinator, TaskAborted
from cabal.core.auth.jwt import JWTError, verify
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.bulletin import store as bulletin_store
from cabal.core.bulletin.store import AgentRow
from cabal.core.db import open_session
from cabal.core.hooks.registry import HookName, HookRegistry

log = structlog.get_logger(__name__)


class DispatchError(RuntimeError):
    """Routing-layer failures: missing target, JWT invalid, transport error."""


class AgentNotFoundError(DispatchError):
    """Target agent has no registered or stored bulletin entry."""


class CallerJWTError(DispatchError):
    """Caller JWT failed verification at the router boundary."""


class ConfigurationError(DispatchError):
    """Agent is declared local but no handler is registered."""


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


def _canceled(task: Task, reason: str) -> Task:
    """Build a canceled Task for a cooperatively-aborted dispatch (cabal#11)."""
    metadata = dict(task.metadata)
    metadata["cabal_reason_message"] = reason
    return Task(
        id=task.id,
        context_id=task.context_id,
        status=TaskStatus(
            state="canceled",
            message=Message(role="agent", parts=[TextPart(text=f"canceled: {reason}")]),
        ),
        history=list(task.history),
        metadata=metadata,
    )


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
        hook_registry: HookRegistry | None = None,
        abort_coordinator: AbortCoordinator | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bookkeeper = bookkeeper
        self._jwks_provider = jwks_provider
        self._http_client = http_client
        # cabal#11 — shared AbortGraph. Owned here so personas (via the
        # factory) and the admin surface all reference one graph.
        self._abort = abort_coordinator or AbortCoordinator()
        self._local: dict[str, LocalHandler] = {}
        # Capability tags per agent — populated at startup from identity.toml.
        # Used by select_by_capabilities() for late-binding task dispatch.
        self._capabilities: dict[str, set[str]] = {}
        self._a2a_path = a2a_path
        self._hooks = hook_registry or HookRegistry()

    @property
    def abort_coordinator(self) -> AbortCoordinator:
        """The shared AbortGraph coordinator (cabal#11)."""
        return self._abort

    # --- registration -----------------------------------------------------

    def register_local(self, handler: LocalHandler) -> None:
        """Wire a LocalHandler into the in-process registry."""
        self._local[handler.name] = handler
        log.info("router.register_local", agent=handler.name)

    def has_local(self, name: str) -> bool:
        return name in self._local

    def get_local(self, name: str) -> LocalHandler | None:
        """Return the local handler for `name`, or None if not registered."""
        return self._local.get(name)

    def unregister_local(self, name: str) -> None:
        self._local.pop(name, None)
        self._capabilities.pop(name, None)

    # --- capability registry ---------------------------------------------

    def register_capabilities(self, agent: str, capabilities: list[str]) -> None:
        """Advertise an agent's capability tags.

        Idempotent — replaces any prior set for this agent. Empty list
        means "no capability tags" (the agent is still dispatchable by
        explicit target, just not by required_capabilities).
        """
        self._capabilities[agent] = {c for c in capabilities if c}
        log.info(
            "router.capabilities_registered",
            agent=agent,
            capabilities=sorted(self._capabilities[agent]),
        )

    def capabilities_for(self, agent: str) -> set[str]:
        return set(self._capabilities.get(agent, set()))

    def select_by_capabilities(self, required: list[str]) -> str | None:
        """Return an agent whose capability set is a superset of `required`.

        Today picks the first match deterministically by name. Future
        ranking (cost / circuit-breaker / round-robin) is a follow-up.

        Returns None when no registered agent satisfies the requirement.
        Empty `required` is meaningless here; callers should pass an
        explicit target in that case.
        """
        if not required:
            return None
        required_set = {r for r in required if r}
        if not required_set:
            return None
        candidates = sorted(
            name for name, caps in self._capabilities.items() if required_set.issubset(caps)
        )
        return candidates[0] if candidates else None

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
        # cabal#11 — register this invocation in the AbortGraph. Parent comes
        # from cabal_parent_task (set by Rook fan-out); top-level tasks have none.
        self._abort.register(
            task.id, parent_id=task.metadata.get("cabal_parent_task"), kind=KIND_DISPATCH
        )
        await self._bookkeeper.on_dispatch_start(task, ctx.target, ctx.caller_agent)

        is_session_root = "cabal_session_started" not in task.metadata
        if is_session_root:
            task.metadata["cabal_session_started"] = True
            await self._hooks.fire(
                HookName.SESSION_START,
                {
                    "task_id": task.id,
                    "target": ctx.target.name,
                    "caller": ctx.caller_agent,
                    "trace_id": task.metadata.get("cabal_trace_id"),
                },
            )

        try:
            # A `federated` target is a sovereign peer — always route remote,
            # even if a local handler is registered for the name (the persona
            # factory registers one for every identity.toml persona; the future
            # `cbl persona graduate` will unregister it, but this guard is the
            # enforcement regardless). NOTE: the guard keys on the *resolved*
            # row's kind, and `target_generation` is caller-controllable — so the
            # mint site (routes/tasks._resolve_dispatch_token) and _prepare must
            # resolve the SAME row for their audience/kind to agree. Both use
            # get_active today; keep it that way if you change audience resolution.
            if ctx.target.name in self._local and ctx.target.kind != "federated":
                response = await self._dispatch_local(self._local[ctx.target.name], task)
            else:
                response = await self._dispatch_remote(ctx.target, task, caller_jwt)
        except TaskAborted as exc:
            # Cooperative cancellation reached the dispatch boundary — surface
            # a clean canceled Task rather than a 500. A checkpoint was written
            # at the abort point, so this Task is resumable via /admin.
            log.info("dispatch.aborted", task_id=task.id, reason=exc.reason)
            response = _canceled(task, exc.reason)
        except Exception as exc:
            await self._bookkeeper.on_dispatch_failure(task, exc)
            if is_session_root:
                await self._hooks.fire(
                    HookName.SESSION_END,
                    {
                        "task_id": task.id,
                        "state": "failed",
                        "error_type": type(exc).__name__,
                    },
                )
            raise
        finally:
            # The invocation is no longer in-flight; drop it from the graph.
            self._abort.complete(task.id)

        await self._bookkeeper.on_dispatch_success(task, response)
        if is_session_root and response.status.state in {
            "completed",
            "failed",
            "canceled",
            "rejected",
        }:
            await self._hooks.fire(
                HookName.SESSION_END,
                {
                    "task_id": task.id,
                    "state": response.status.state,
                    "reason_code": response.metadata.get("cabal_reason_code"),
                },
            )
        return response

    async def dispatch_streaming(
        self,
        *,
        target_agent: str,
        target_generation: int | None,
        task: Task,
        caller_jwt: str,
    ) -> AsyncIterator[str]:
        """Like dispatch() but yields string deltas from astream_task.

        Enforces the same §6.12 invariants as dispatch(): JWT re-verification,
        Conversation/Event bookkeeping, session hooks, and annotated failure
        Tasks on exception. Streaming callers get the invariants; they just
        receive chunks instead of a single returned Task.

        Raises DispatchError (before yielding) if the target has no local
        handler or its handler doesn't implement astream_task.
        """
        ctx = await self._prepare(target_agent, target_generation, caller_jwt)

        # Symmetric with dispatch()'s guard: a federated target is a sovereign
        # peer and must never be served locally — even via streaming, even if a
        # stale local handler is registered for the name. The sovereign peer has
        # no streaming surface here, so this is a hard refusal, not a fallthrough.
        if ctx.target.kind == "federated":
            raise DispatchError(
                f"agent {target_agent!r} is federated; streaming to a sovereign "
                "peer is not supported — dispatch via /a2a/tasks instead"
            )

        if target_agent not in self._local:
            raise DispatchError(
                f"agent {target_agent!r} has no in-process handler "
                "(streaming requires a local persona)"
            )
        handler = self._local[target_agent]
        if not hasattr(handler, "astream_task"):
            raise DispatchError(f"agent {target_agent!r}'s handler does not implement astream_task")

        task = _ensure_trace(task)
        await self._bookkeeper.on_dispatch_start(task, ctx.target, ctx.caller_agent)

        is_session_root = "cabal_session_started" not in task.metadata
        if is_session_root:
            task.metadata["cabal_session_started"] = True
            await self._hooks.fire(
                HookName.SESSION_START,
                {
                    "task_id": task.id,
                    "target": ctx.target.name,
                    "caller": ctx.caller_agent,
                    "trace_id": task.metadata.get("cabal_trace_id"),
                },
            )

        full_text = ""
        try:
            async for delta in handler.astream_task(task):
                full_text += delta
                yield delta
        except Exception as exc:
            await self._bookkeeper.on_dispatch_failure(task, exc)
            if is_session_root:
                await self._hooks.fire(
                    HookName.SESSION_END,
                    {
                        "task_id": task.id,
                        "state": "failed",
                        "error_type": type(exc).__name__,
                    },
                )
            raise

        final_task = Task(
            id=task.id,
            status=TaskStatus(
                state="completed",
                message=Message(
                    role="agent",
                    parts=[TextPart(kind="text", text=full_text)],
                ),
            ),
            metadata=task.metadata,
        )
        await self._bookkeeper.on_dispatch_success(task, final_task)
        if is_session_root:
            await self._hooks.fire(
                HookName.SESSION_END,
                {
                    "task_id": task.id,
                    "state": "completed",
                    "reason_code": final_task.metadata.get("cabal_reason_code"),
                },
            )

    async def resume_local(
        self,
        *,
        target_agent: str,
        task: Task,
        resume_from: Any,
    ) -> Task:
        """Re-enter a previously-checkpointed Task on its local handler.

        cabal#2 — used by POST /admin/tasks/{id}/resume. Bypasses the
        JWT/bulletin/session-start path because the original dispatch
        already ran those; this is a continuation, not a fresh
        dispatch. Bookkeeping fires success/failure exactly like the
        primary path so /admin/conversations/{id} reflects the resumed
        outcome.
        """
        # Symmetric federated guard — a sovereign peer has no local resume
        # surface; reject before touching any handler. Mirrors the federated
        # guard in dispatch_streaming (cabal#45).
        async with open_session(self._sessionmaker) as session:
            _row = await bulletin_store.get_active(session, target_agent)
        if _row is not None and _row.kind == "federated":
            raise DispatchError(
                f"agent {target_agent!r} is federated; resume is not supported "
                "for sovereign peers — only local personas can be resumed"
            )

        if target_agent not in self._local:
            raise DispatchError(
                f"agent {target_agent!r} has no in-process handler "
                "(resume requires a local persona)"
            )
        handler = self._local[target_agent]
        if not hasattr(handler, "handle_task"):
            raise DispatchError(f"agent {target_agent!r}'s handler does not implement handle_task")

        try:
            response = await handler.handle_task(task, resume_from=resume_from)
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
                row = await bulletin_store.get(session, target_agent, target_generation)
        if row is None:
            raise AgentNotFoundError(
                f"no agent registered: {target_agent!r}"
                + (f" generation={target_generation}" if target_generation is not None else "")
            )

        # Invariant 2: caller-JWT verification, even for in-process. For a
        # federated target the same token is forwarded to the sovereign peer, so
        # it must carry (and we verify against) the sovereign service id, not the
        # bare agent name. A federated row without an audience is a
        # misconfiguration — fail loudly rather than silently accept a bare-name
        # aud we'd then forward to a peer that rejects it.
        if row.kind == "federated":
            if not row.federation_audience:
                raise ConfigurationError(
                    f"federated agent {target_agent!r} has no federation_audience"
                )
            expected_aud = row.federation_audience
        else:
            expected_aud = target_agent
        try:
            payload = verify(
                caller_jwt,
                key_set=self._jwks_provider(),
                expected_audience=expected_aud,
            )
        except JWTError as exc:
            raise CallerJWTError(f"caller JWT rejected: {exc}") from exc

        return DispatchContext(
            target=row,
            caller_agent=payload.get("sub"),
            caller_persona=payload.get("cabal_persona"),
        )

    async def _dispatch_local(self, handler: LocalHandler, task: Task) -> Task:
        return await handler.handle_task(task)

    async def _dispatch_remote(self, target: AgentRow, task: Task, caller_jwt: str) -> Task:
        if target.endpoint.startswith("local://"):
            # The bulletin row says "local handler" but none is registered
            # — this is a misconfiguration, not a transport failure.
            raise ConfigurationError(
                f"agent {target.name!r} is declared local but no handler "
                f"is registered (identity.toml persona without an "
                f"implementation in PERSONA_REGISTRY)"
            )
        if self._http_client is None:
            raise DispatchError("remote dispatch attempted without an http client wired in")
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
            raise DispatchError(f"remote dispatch returned HTTP {resp.status_code}: {resp.text}")
        return Task.model_validate(resp.json())
