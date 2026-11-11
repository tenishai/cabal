"""Dispatch router — the keystone of Option C.

§6.12 invariants enforced here:

    1. AgentCard signature verification. NOT enforced: nothing in src/ calls
       `AgentCardVerifier` (core/a2a/card.py), at registration or dispatch.
       The caller's JWT (invariant 2) is the trust (#226).
    2. Caller JWT verification on EVERY dispatch, even in-process.
    3. Broker bookkeeping module on EVERY dispatch (audit + events).
    4. Trace context (cabal_trace_id, cabal_span_id) propagated via
       task.metadata, fresh-generated when absent.

The router is the single place invariants 2-4 live; tests assert each
one independently and assert them identically across solo + scale modes.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import structlog
from joserfc.jwk import KeySet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.agents.base import LocalHandler
from cabal.core.a2a.codes import CODE_POLICY_VIOLATION, CabalReason, annotate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.abort import KIND_DISPATCH, AbortCoordinator, TaskAborted
from cabal.core.auth.jwt import JWTError, verify
from cabal.core.auth.revocations import RevocationList, refusal
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.bulletin import store as bulletin_store
from cabal.core.bulletin.store import AgentRow
from cabal.core.db import open_session
from cabal.core.guardrails import Guardrail, GuardrailContext, VerdictType, run_guardrails
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.running import RunningTask, RunningTasks

log = structlog.get_logger(__name__)


class DispatchError(RuntimeError):
    """Routing-layer failures: missing target, JWT invalid, transport error."""


class AgentNotFoundError(DispatchError):
    """Target agent has no registered or stored bulletin entry."""


class GuardrailBlocked(DispatchError):
    """An input guardrail refused this dispatch (cabal#151, #153 review).

    A SUBCLASS so every existing `except DispatchError` keeps working, and a
    distinct type so the streaming route can report it as ERROR_FORBIDDEN
    rather than ERROR_INTERNAL. That distinction is not cosmetic: -32603 says
    "server fault", and the conventional response to a server fault is to
    retry -- so reporting a guardrail refusal as one sustains, at the
    transport layer, the loop the guardrail exists to break.
    """


class DrainingError(DispatchError):
    """cabal is draining (issue 405) and this dispatch would start new work.

    A subclass so every existing `except DispatchError` still refuses it, and
    a distinct type so each route reports it as "retry later", not as a
    server fault."""


class TaskAlreadyRunning(DispatchError):
    """The task id names a dispatch that is still running (PR 407 round 2).

    A client's retry with the same id while the first run is going. Refused
    before anything is written; /a2a/tasks answers 409, since retrying the
    same id can't succeed until the first run ends."""


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


@dataclass(slots=True)
class _Started:
    """A dispatch past its start phase: what the run phase needs."""

    ctx: DispatchContext
    task: Task
    is_session_root: bool
    target_agent: str
    caller_jwt: str
    running_entry: RunningTask | None = None


def _with_context(task: Task, response: Task) -> Task:
    """`response` as it is stored, for ListTasks and A2A context replay (#276).

    The dispatched task's contextId goes back on when the reply has none
    (EchoAgent drops it). And a reply whose history is no longer than the
    input's is stored with the input history plus the incoming message
    instead. That covers cabal's own replies (a cancel, a guardrail block),
    which carry only the earlier history, and any reply that sends no history
    at all, a remote peer's included. A local persona's reply is one longer
    (the earlier history plus the incoming message) and is stored as it came,
    as is any other reply with a longer history. Length, not value equality:
    the same message can appear twice in one conversation.
    """
    update: dict[str, object] = {}
    if response.context_id is None and task.context_id is not None:
        update["context_id"] = task.context_id
    incoming = task.status.message
    if incoming is not None and len(response.history) <= len(task.history):
        update["history"] = [*task.history, incoming]
    return response.model_copy(update=update) if update else response


def _canceled(task: Task, reason: str) -> Task:
    """Build a canceled Task for a cooperatively-aborted dispatch (cabal#11)."""
    metadata = dict(task.metadata)
    metadata["cabal_reason_message"] = reason
    return Task(
        id=task.id,
        # cabal#59: `contextId` alias — `context_id=` silently drops (see llm_persona).
        contextId=task.context_id,
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
        input_guardrails: list[Guardrail] | None = None,
        revocations: RevocationList | None = None,
        running: RunningTasks | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bookkeeper = bookkeeper
        self._jwks_provider = jwks_provider
        # Subject-wide revocations; None means "none configured" (tests that
        # build a router bare), never "skip the check" on a running daemon.
        self._revocations = revocations
        self._http_client = http_client
        # cabal#11 — shared AbortGraph. Owned here so personas (via the
        # factory) and the admin surface all reference one graph.
        self._abort = abort_coordinator or AbortCoordinator()
        # cabal issue 405 — the dispatches running now, and the drain flag.
        self._running = running or RunningTasks()
        self._local: dict[str, LocalHandler] = {}
        # Capability tags per agent — populated at startup from identity.toml.
        # Used by select_by_capabilities() for late-binding task dispatch.
        self._capabilities: dict[str, set[str]] = {}
        self._a2a_path = a2a_path
        self._hooks = hook_registry or HookRegistry()
        # cabal#39 — router-level input guardrails applied before the A2A hop.
        # None/empty = no-op (default); adding guardrails is opt-in per deployment.
        self._input_guardrails: list[Guardrail] = input_guardrails or []

    @property
    def abort_coordinator(self) -> AbortCoordinator:
        """The shared AbortGraph coordinator (cabal#11)."""
        return self._abort

    @property
    def running(self) -> RunningTasks:
        """The running tasks and the drain state (cabal issue 405)."""
        return self._running

    def _admit(self, ctx: DispatchContext) -> None:
        """Refuse new work while draining, unless the caller is an agent
        that is itself running a task (issue 405)."""
        if not self._running.admits(ctx.caller_agent):
            log.info("dispatch.refused_draining", target=ctx.target.name, caller=ctx.caller_agent)
            raise DrainingError(
                "cabal is draining before a restart and isn't starting new work; "
                "retry after it restarts"
            )

    def _track(
        self, task: Task, ctx: DispatchContext, *, detached: bool = False, streaming: bool = False
    ) -> RunningTask:
        """List the task. An id that is already running is refused here, before
        anything is written: replacing its entry, then removing it when this
        dispatch's start fails, would unlist work that is still going (PR 407
        round 2)."""
        if task.id in self._running:
            log.info("dispatch.refused_duplicate", task_id=task.id, target=ctx.target.name)
            raise TaskAlreadyRunning(f"task {task.id!r} is already running; use a new task id")
        entry = RunningTask(
            task_id=task.id,
            target_agent=ctx.target.name,
            caller_agent=ctx.caller_agent,
            started_at=datetime.now(UTC),
            detached=detached,
            streaming=streaming,
            parent_task=task.metadata.get("cabal_parent_task"),
        )
        self._running.add(entry)
        return entry

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

    def input_guardrail_of[T](self, kind: type[T]) -> T | None:
        """The router's own instance of `kind`, or None (cabal#158).

        Exists so a caller can SHARE a guard rather than construct a second
        one. The delegation budget is per-conversation and keyed on
        context_id, so two instances would hand one conversation two separate
        allowances -- an agent mixing router hops with primus fan-out legs
        would get double the budget while looking bounded.
        """
        for guard in self._input_guardrails:
            if isinstance(guard, kind):
                return guard
        return None

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
        started = await self._start_dispatch(target_agent, target_generation, task, caller_jwt)
        return await self._run_dispatch(started)

    async def start_detached(
        self,
        *,
        target_agent: str,
        target_generation: int | None,
        task: Task,
        caller_jwt: str,
    ) -> tuple[Task, Coroutine[Any, Any, Task]]:
        """Begin a dispatch whose persona call runs later (#277).

        Runs the start phase now, auth included, so a refused token is refused
        here and the task's row exists in `submitted` before this returns.
        Returns the task and the run phase as a coroutine for the caller to
        hand to its owner (the app's detached-task registry). Cancel it through
        the AbortCoordinator, like any dispatch.
        """
        started = await self._start_dispatch(
            target_agent, target_generation, task, caller_jwt, detached=True
        )
        return started.task, self._run_dispatch(started)

    async def _start_dispatch(
        self,
        target_agent: str,
        target_generation: int | None,
        task: Task,
        caller_jwt: str,
        *,
        detached: bool = False,
    ) -> _Started:
        """Everything before the persona call: auth, trace ids, the AbortGraph
        node, the conversations row, the session hook."""

        ctx = await self._prepare(target_agent, target_generation, caller_jwt)
        self._admit(ctx)
        task = _ensure_trace(task)
        # Listed from the moment it is admitted (issue 405, PR 407 review): a drain
        # that starts while the row is being written must still see it.
        entry = self._track(task, ctx, detached=detached)
        try:
            # cabal#11 — register this invocation in the AbortGraph. Parent comes
            # from cabal_parent_task (set by Mason fan-out); top-level tasks have none.
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
        except BaseException:
            self._running.remove(task.id, entry)
            raise
        return _Started(
            running_entry=entry,
            ctx=ctx,
            task=task,
            is_session_root=is_session_root,
            target_agent=target_agent,
            caller_jwt=caller_jwt,
        )

    async def _run_dispatch(self, started: _Started) -> Task:
        """Router guardrails, the persona call, and the success or failure
        bookkeeping. Records `working` just before the persona is called.
        The task stays on the running list until its result is written and
        SESSION_END has fired, so an empty list means nothing is left to
        lose (issue 405, PR 407 review)."""
        try:
            return await self._run_dispatch_body(started)
        finally:
            self._running.remove(started.task.id, started.running_entry)

    async def _run_dispatch_body(self, started: _Started) -> Task:
        ctx, task, is_session_root = started.ctx, started.task, started.is_session_root
        target_agent, caller_jwt = started.target_agent, started.caller_jwt
        try:
            # cabal#39 — router-level input guardrail check before the A2A hop.
            # A BLOCK here prevents dispatch entirely and surfaces a
            # policy-violation Task; a REWRITE replaces the Task envelope
            # before it reaches the handler.
            response: Task | None = None
            if self._input_guardrails:
                guard_ctx = GuardrailContext(
                    persona_name=ctx.caller_persona or ctx.caller_agent or "",
                    task_id=task.id,
                    phase="router",
                    caller_persona=ctx.caller_persona,
                    caller_agent=ctx.caller_agent,
                )
                # cabal#42 — expose the target agent name to router-level
                # guardrails (e.g. DoomLoopGuardrail) via task metadata.
                # Consistent with cabal_trace_id / cabal_span_id convention.
                task.metadata["cabal_target_agent"] = target_agent
                verdict = await run_guardrails(self._input_guardrails, task, guard_ctx)
                if verdict.type == VerdictType.TERMINATE:
                    # ENDS THE TASK (cabal#152). GuardrailBlocked is the same
                    # signal #153 raises from dispatch_streaming, but the
                    # FORBIDDEN wiring was streaming-only: every caller of this
                    # non-streaming method caught the DispatchError base and
                    # answered 502 / ERROR_INTERNAL -- "server fault, retry" --
                    # for a refusal that no retry changes. An earlier version of
                    # this comment claimed the distinction already applied here;
                    # it did not, and the #152 pre-pass caught it. Both callers
                    # now have their own arm: routes/tasks.py (403) and
                    # a2a_jsonrpc._message_send (ERROR_FORBIDDEN). Add one to any
                    # third caller, or it will inherit 502 silently.
                    # A BLOCK below still advises.
                    #
                    # NO on_dispatch_failure CALL HERE, unlike the streaming
                    # path this was copied from (#152 r1 Important 1). This
                    # raise is INSIDE the try opened above, whose `except
                    # Exception` already closes the record. on_dispatch_failure
                    # inserts an Event and publishes to the bus, so calling it
                    # here too wrote the failure twice -- measured as
                    # ['dispatch_start', 'dispatch_failure', 'dispatch_failure']
                    # against the streaming path's two. The Conversation update
                    # is an idempotent state="failed", which is why nothing
                    # else showed it. dispatch_streaming raises from OUTSIDE
                    # its own try, so its explicit call is correct where it is.
                    raise GuardrailBlocked(
                        f"router guardrail terminated the task: {verdict.reason}"
                    )
                if verdict.type == VerdictType.BLOCK:
                    response = annotate(
                        task,
                        CabalReason(
                            code=CODE_POLICY_VIOLATION,
                            message=f"router guardrail blocked: {verdict.reason}",
                        ),
                    )
                elif verdict.rewritten is not None:
                    task = verdict.rewritten  # use the guardrail-rewritten Task

            if response is None:
                # A `federated` target is a sovereign peer — always route remote,
                # even if a local handler is registered for the name (the persona
                # factory registers one for every identity.toml persona; the future
                # `cbl persona graduate` will unregister it, but this guard is the
                # enforcement regardless). NOTE: the guard keys on the *resolved*
                # row's kind, and `target_generation` is caller-controllable. The
                # mint site (_dispatch_auth.dispatch_token, via dispatch_audience)
                # takes the audience from the ACTIVE row; _prepare resolves the
                # PINNED row when target_generation is set. If those rows disagree,
                # _prepare's caller-JWT check against its own row's audience refuses
                # the dispatch (CallerJWTError), so a mismatch fails closed. Keep that
                # check if you change how either site resolves the row.
                await self._bookkeeper.on_dispatch_working(task)
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

        response = _with_context(task, response)
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
        """Like dispatch() but yields string deltas; see `_stream`. The task
        is on the running list (issue 405) from its start until the stream
        ends, however it ends. It removes only an entry this stream added, so a
        stream refused for reusing a running task's id leaves that task listed."""
        before = self._running.get(task.id)
        try:
            async for delta in self._stream(
                target_agent=target_agent,
                target_generation=target_generation,
                task=task,
                caller_jwt=caller_jwt,
            ):
                yield delta
        finally:
            ours = self._running.get(task.id)
            if ours is not None and ours is not before:
                self._running.remove(task.id, ours)

    async def _stream(
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
        self._admit(ctx)
        # Listed from admission; dispatch_streaming's finally removes it.
        self._track(task, ctx, streaming=True)

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

        # cabal#151 — the SAME input-guardrail chain dispatch() runs. Without
        # this, every router-level bound was opt-out by choosing the streaming
        # endpoint, which makes a bound not a bound. On this branch that is
        # DoomLoopGuardrail (#42); the delegation budget (#146) joins it when
        # that branch merges, and would have shipped bypassable without this.
        #
        # AFTER the structural refusals above, deliberately: a guardrail that
        # ran first would charge a delegation budget for a dispatch that could
        # never have happened (unknown agent, no astream_task). dispatch() has
        # the same ordering -- _prepare resolves and raises before any guard.
        #
        # BLOCK raises rather than yielding an error delta. The three refusals
        # above already establish "this stream will not happen" as a pre-yield
        # raise, and both callers already handle DispatchError -- the SSE route
        # emits an error frame carrying partial_text (a2a_jsonrpc.py), the TUI
        # catches it. Yielding a delta instead would mean a blocked stream had
        # opened, which is the opposite of an *input* guardrail's contract.
        # BOOKKEEPING OPENS BEFORE THE GUARD, matching dispatch() (#153 review).
        # dispatch() calls on_dispatch_start before its own guard, and a
        # BLOCK there then falls
        # through to on_dispatch_success, which writes the annotated
        # CODE_POLICY_VIOLATION task into Conversation.result_json with an Event
        # row -- so a non-streaming refusal is fully audited. Guarding first
        # here would have made streaming the one path where a refusal leaves no
        # trace, which is the fail-silent shape #148 removed from the presence
        # hook. It also contradicted this method's own docstring promise of
        # "Conversation/Event bookkeeping".
        task = _ensure_trace(task)
        await self._bookkeeper.on_dispatch_start(task, ctx.target, ctx.caller_agent)

        if self._input_guardrails:
            guard_ctx = GuardrailContext(
                persona_name=ctx.caller_persona or ctx.caller_agent or "",
                task_id=task.id,
                phase="router",
                caller_persona=ctx.caller_persona,
                caller_agent=ctx.caller_agent,
            )
            # Same metadata contract as dispatch(): DoomLoopGuardrail keys its
            # delegation hash on this and would otherwise hash an empty target.
            task.metadata["cabal_target_agent"] = target_agent
            verdict = await run_guardrails(self._input_guardrails, task, guard_ctx)
            if verdict.type in (VerdictType.BLOCK, VerdictType.TERMINATE):
                # The streaming path has no advisory mode: there is no partial
                # Task to annotate and hand back, so a BLOCK already ends the
                # stream. TERMINATE lands here identically, and the wording
                # follows the verdict so the caller can tell which it was.
                word = (
                    "terminated the task"
                    if verdict.type == VerdictType.TERMINATE
                    else "blocked"
                )
                blocked = GuardrailBlocked(f"router guardrail {word}: {verdict.reason}")
                # CLOSE the record we just opened. Without this the refusal
                # leaves a started-and-never-finished dispatch, which is worse
                # than no row: it reads as an in-flight call that vanished.
                await self._bookkeeper.on_dispatch_failure(task, blocked)
                raise blocked
            if verdict.rewritten is not None:
                task = verdict.rewritten

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
            # The input history plus the input message, as a persona's own
            # reply carries them, so the stored task holds the conversation
            # up to it for A2A context replay (#276).
            history=[*task.history, *([task.status.message] if task.status.message else [])],
            metadata=task.metadata,
        )
        final_task = _with_context(task, final_task)
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

        # An operator's resume is listed while it runs (issue 405), but not
        # refused by a drain: the operator asked for it. It is refused if the
        # task is still running (PR 407 round 3): a checkpointing persona has a
        # row and a checkpoint mid-run.
        if task.id in self._running:
            raise TaskAlreadyRunning(f"task {task.id!r} is still running; resume it once it has stopped")
        entry = RunningTask(
            task_id=task.id,
            target_agent=target_agent,
            caller_agent=None,
            started_at=datetime.now(UTC),
            parent_task=task.metadata.get("cabal_parent_task"),
        )
        self._running.add(entry)
        try:
            try:
                response = await handler.handle_task(task, resume_from=resume_from)
            except Exception as exc:
                await self._bookkeeper.on_dispatch_failure(task, exc)
                raise
            response = _with_context(task, response)
            await self._bookkeeper.on_dispatch_success(task, response)
            return response
        finally:
            # After the result is written, as in _run_dispatch.
            self._running.remove(task.id, entry)

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
            # A role="primus" persona registers as "<name>-primus", so the
            # natural `--persona rook` misses (cabal#95). Name the real agent
            # in the miss instead of leaving the suffix to be guessed. Only
            # for an unpinned lookup: the suggestion is an ACTIVE row, which
            # says nothing about a pinned generation. It reveals nothing new
            # either: this miss is answered before the caller JWT is checked
            # against the target, so any caller that reaches the router can
            # already probe "<name>-primus" by name.
            suggestion = None
            if (
                row is None
                and target_generation is None
                and not target_agent.endswith("-primus")
            ):
                primus_name = f"{target_agent}-primus"
                if await bulletin_store.get_active(session, primus_name) is not None:
                    suggestion = primus_name
        if row is None:
            raise AgentNotFoundError(
                f"no agent registered: {target_agent!r}"
                + (f" generation={target_generation}" if target_generation is not None else "")
                + (
                    f"; did you mean {suggestion!r}? A primus persona registers "
                    "under a '-primus' suffix"
                    if suggestion
                    else ""
                )
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
        if (why := refusal(self._revocations, payload)) is not None:
            raise CallerJWTError(f"caller JWT rejected: {why}")

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
