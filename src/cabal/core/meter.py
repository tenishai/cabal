"""The token meter for federated agents (cabal issue 394, part 2).

A federated agent (a daedalus worker started by ergon) makes its model calls in
its own process, so cabal never saw what it spent, only what it said it spent.
With `[meter]` on, ergon points the agent's model base URL at
`<cabal>/meter/<key>`, and each call passes through here:

    agent --POST /meter/<key>/v1/messages--> cabal --forward--> upstream
          <------------- response, unchanged but decompressed -----

The key names the agent (identity.toml keeps only its SHA-256). The agent's
running task, from `RunningTasks`, names what the call is charged to, and the
task's root is found by following the verified caller up the running list. The
usage is read from the response: Anthropic's `message_start` and
`message_delta` stream events or a JSON body's `usage`, and OpenAI's `usage`
block. Only input and output tokens are charged; cache tokens are recorded and
never charged (Rich, 2026-11-16).

PASS-THROUGH ONLY. The agents run on the operator's own logins, and Anthropic's
terms allow a gateway that forwards `Authorization` and `anthropic-beta`
unchanged. The meter never reads, logs, stores, adds or swaps a credential.

An agent running two tasks at once can't have a call pinned to either, so the
call is forwarded, counted in the agent's `ambiguous` row, and charged to
nothing (Rich, 2026-11-16: no refusal of a second task). A call while the agent
runs nothing is counted as `unattributed`.

With `task_budget` set, a task whose budget is spent has its calls refused with
a 400 `invalid_request_error`: measured on Claude Code 2.1.325, a 400 stops it at
once, while 429 and 5xx are retried for about three minutes.

Declared state, in the shape cabal issue 260 asks for:
    what: per-task budgets (an own BudgetGuard, in memory) and `meter_spend` rows
    written by: the /meter route (calls), RunningTasks.on_remove (task end)
    reason: measure what federated workers spend instead of trusting their reports
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from cabal.config.identity import MeterConfig
from cabal.core.db import open_session
from cabal.core.llm.budget import BudgetExhaustedError, BudgetGuard
from cabal.core.models import MeterSpend
from cabal.core.running import RunningTask, RunningTasks, _subject_agents

log = structlog.get_logger(__name__)

KIND_TASK = "task"
KIND_UNATTRIBUTED = "unattributed"
KIND_AMBIGUOUS = "ambiguous"

#: Not forwarded either way. `accept-encoding` is replaced with `identity` so the
#: upstream answers uncompressed; a compressed body passed on with its header
#: dropped breaks Claude Code ("JSON Parse error: Unrecognized token").
_HOP = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "content-length",
        "content-encoding",
        "accept-encoding",
    }
)
#: The most of a non-streamed body kept for reading its usage.
_JSON_CAP = 16 * 1024 * 1024
#: How far up the running list a root is looked for.
_ROOT_DEPTH = 16


def key_sha256(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0

    @property
    def charged(self) -> int:
        """What counts against a budget: input and output, never cache."""
        return self.input + self.output

    def take_max(self, other: Usage) -> None:
        """Anthropic's stream repeats counts cumulatively (`message_start`, then
        `message_delta`), so the largest value seen for each field is the total."""
        self.input = max(self.input, other.input)
        self.output = max(self.output, other.output)
        self.cache_write = max(self.cache_write, other.cache_write)
        self.cache_read = max(self.cache_read, other.cache_read)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def usage_from_block(block: Any) -> Usage | None:
    """A `usage` object in either shape. Anthropic's `input_tokens` already
    excludes cache tokens. OpenAI's `prompt_tokens` includes the cached ones,
    so they are taken out of input and counted as cache reads."""
    if not isinstance(block, dict):
        return None
    if "input_tokens" in block or "output_tokens" in block:
        return Usage(
            input=_int(block.get("input_tokens")),
            output=_int(block.get("output_tokens")),
            cache_write=_int(block.get("cache_creation_input_tokens")),
            cache_read=_int(block.get("cache_read_input_tokens")),
        )
    if "prompt_tokens" in block or "completion_tokens" in block:
        details = block.get("prompt_tokens_details")
        cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        prompt = _int(block.get("prompt_tokens"))
        return Usage(
            input=max(0, prompt - cached),
            output=_int(block.get("completion_tokens")),
            cache_read=cached,
        )
    return None


def usage_from_event(obj: Any) -> Usage | None:
    """The usage in one parsed JSON body or stream event, if it has any."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "message_start" and isinstance(obj.get("message"), dict):
        return usage_from_block(obj["message"].get("usage"))
    return usage_from_block(obj.get("usage"))


class UsageReader:
    """Reads the usage out of a response as its bytes pass through."""

    def __init__(self, content_type: str) -> None:
        self.stream = "event-stream" in content_type
        self.usage = Usage()
        self.seen = False
        self._buf = bytearray()
        self._capped = False

    def feed(self, chunk: bytes) -> None:
        if self.stream:
            self._buf.extend(chunk)
            while (nl := self._buf.find(b"\n")) != -1:
                line = bytes(self._buf[:nl]).strip()
                del self._buf[: nl + 1]
                self._line(line)
        elif not self._capped:
            self._buf.extend(chunk)
            if len(self._buf) > _JSON_CAP:
                self._capped = True
                self._buf.clear()

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            self._take(json.loads(data))
        except ValueError:
            return

    def _take(self, obj: Any) -> None:
        found = usage_from_event(obj)
        if found is not None:
            self.seen = True
            self.usage.take_max(found)

    def finish(self) -> Usage | None:
        if self.stream:
            if self._buf:
                self._line(bytes(self._buf).strip())
                self._buf.clear()
        elif self._buf and not self._capped:
            try:
                self._take(json.loads(bytes(self._buf)))
            except ValueError:
                pass
        return self.usage if self.seen else None


@dataclass(frozen=True)
class Charge:
    """Who a call is charged to."""

    agent: str
    kind: str
    task_id: str = ""
    root_id: str | None = None


def _error(message: str, status: int = 400) -> JSONResponse:
    """Anthropic's error shape; OpenAI clients read `error.message` from it too."""
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return JSONResponse(body, status_code=status)


class Meter:
    def __init__(
        self,
        config: MeterConfig,
        running: RunningTasks,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.running = running
        self.sessionmaker = sessionmaker
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=10.0)
        )
        self._owns_client = client is None
        # Its own guard, so a task id here never meets one of the in-process
        # personas' budgets (app.state.budget_guard).
        self.budget = BudgetGuard()
        self._by_hash = {a.key_sha256: (name, a.upstream) for name, a in config.agents.items()}
        #: task id -> root id, for tasks whose budget is open (root == task: no leg).
        self._open: dict[str, str] = {}
        #: Roots given `root_budget` here, dropped when the root task ends.
        self._roots: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()
        running.on_remove.append(self.task_ended)

    async def aclose(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        if self._owns_client:
            await self.client.aclose()

    # -- who --------------------------------------------------------------

    def agent_for(self, key: str) -> tuple[str, str] | None:
        """(agent, upstream) for a key, or None."""
        digest = key_sha256(key)
        for known, found in self._by_hash.items():
            if hmac.compare_digest(known, digest):
                return found
        return None

    def _tasks_of(self, sub: str) -> list[RunningTask]:
        """The running tasks of an agent, by name or token subject (`agent:<name>`)."""
        names = _subject_agents(sub)
        return [t for t in self.running.list() if t.target_agent in names]

    def root_of(self, task: RunningTask) -> str:
        """Follow the task up the running list: to the task cabal stamped as its
        parent, else to the one task its verified caller is running. Stops where
        neither is known, or at a caller running several tasks."""
        seen = {task.task_id}
        current = task
        for _ in range(_ROOT_DEPTH):
            parent: RunningTask | None = None
            if current.parent_task:
                parent = self.running.get(current.parent_task)
            elif current.caller_agent:
                callers = self._tasks_of(current.caller_agent)
                if len(callers) == 1:
                    parent = callers[0]
            if parent is None or parent.task_id in seen:
                break
            seen.add(parent.task_id)
            current = parent
        return current.task_id

    def charge_for(self, agent: str) -> Charge:
        tasks = self._tasks_of(agent)
        if not tasks:
            return Charge(agent, KIND_UNATTRIBUTED)
        if len(tasks) > 1:
            return Charge(agent, KIND_AMBIGUOUS)
        task = tasks[0]
        return Charge(agent, KIND_TASK, task.task_id, self.root_of(task))

    # -- budget -----------------------------------------------------------

    def admit(self, charge: Charge) -> str | None:
        """None if the call may go, else why it is refused."""
        if charge.kind != KIND_TASK or self.config.task_budget is None:
            return None
        task, root = charge.task_id, charge.root_id or charge.task_id
        if task not in self._open:
            try:
                self._open_budget(task, root)
            except BudgetExhaustedError as exc:
                return f"cabal meter: {exc}"
        left = self.budget.remaining(task)
        if left <= 0:
            return (
                f"cabal meter: task {task!r} has spent its token budget "
                f"({self.config.task_budget} input + output tokens)"
            )
        return None

    def _open_budget(self, task: str, root: str) -> None:
        allotment = self.config.task_budget
        assert allotment is not None
        if root != task and self.config.root_budget is not None:
            if not self.budget.has_budget(root):
                self.budget.set_budget(root, self.config.root_budget)
                self._roots.add(root)
            self.budget.open_leg(task, root, allotment)
        else:
            self.budget.set_budget(task, allotment)
        self._open[task] = root
        log.info("meter.budget_opened", task_id=task, root_id=root, allotment=allotment)

    def task_ended(self, task: RunningTask) -> None:
        root = self._open.pop(task.task_id, None)
        if root is not None:
            if root != task.task_id and root in self._roots:
                self.budget.close_leg(task.task_id)
            else:
                self.budget.reset(task.task_id)
        if task.task_id in self._roots:
            self._roots.discard(task.task_id)
            self.budget.reset(task.task_id)
        if task.target_agent in self.config.agents:
            self._spawn(self._close_row(task.target_agent, task.task_id))

    # -- forwarding -------------------------------------------------------

    async def forward(self, agent: str, upstream: str, path: str, request: Request) -> Response:
        url = upstream.rstrip("/") + "/" + path
        if request.url.query:
            url += "?" + request.url.query
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in _HOP]
        headers.append(("accept-encoding", "identity"))
        body = await request.body()
        # Only POSTs are model calls; Claude Code also probes the base with GET and HEAD.
        metered = request.method == "POST"
        charge = self.charge_for(agent) if metered else None
        if charge is not None:
            refusal = self.admit(charge)
            if refusal is not None:
                log.warning("meter.refused", agent=agent, task_id=charge.task_id, reason=refusal)
                self._spawn(self._record(charge, None, refused=True))
                return _error(refusal)
        try:
            upstream_req = self.client.build_request(
                request.method, url, headers=headers, content=body
            )
            resp = await self.client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            log.warning("meter.upstream_failed", agent=agent, error=type(exc).__name__)
            return _error(f"cabal meter: upstream unreachable ({type(exc).__name__})", status=502)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}
        reader = UsageReader(resp.headers.get("content-type", ""))

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    if charge is not None:
                        reader.feed(chunk)
                    yield chunk
            finally:
                await resp.aclose()
                if charge is not None:
                    usage = reader.finish()
                    log.debug(
                        "meter.call",
                        agent=agent,
                        kind=charge.kind,
                        task_id=charge.task_id,
                        status=resp.status_code,
                        usage=None if usage is None else usage.__dict__,
                    )
                    if (
                        usage is not None
                        and charge.kind == KIND_TASK
                        and charge.task_id in self._open
                    ):
                        self.budget.refund(charge.task_id, usage.charged, reserved=0)
                    self._spawn(self._record(charge, usage))

        return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)

    # -- the record -------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        """Writes run as their own tasks, so a client that hangs up mid-stream
        doesn't cancel the record of what the call spent."""
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _row(self, session: AsyncSession, charge: Charge) -> MeterSpend:
        row = (
            await session.execute(
                select(MeterSpend).where(
                    MeterSpend.agent == charge.agent,
                    MeterSpend.kind == charge.kind,
                    MeterSpend.task_id == charge.task_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = MeterSpend(
                id=uuid.uuid4().hex,
                agent=charge.agent,
                kind=charge.kind,
                task_id=charge.task_id,
                root_id=charge.root_id,
                calls=0,
                refused_calls=0,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
            )
            session.add(row)
        return row

    async def _record(self, charge: Charge, usage: Usage | None, *, refused: bool = False) -> None:
        try:
            async with self._write_lock, open_session(self.sessionmaker) as session:
                row = await self._row(session, charge)
                if refused:
                    row.refused_calls += 1
                else:
                    row.calls += 1
                if usage is not None:
                    row.input_tokens += usage.input
                    row.output_tokens += usage.output
                    row.cache_write_tokens += usage.cache_write
                    row.cache_read_tokens += usage.cache_read
                row.updated_at = datetime.now(UTC)
        except Exception:
            log.exception("meter.record_failed", agent=charge.agent, task_id=charge.task_id)

    async def _close_row(self, agent: str, task_id: str) -> None:
        try:
            async with self._write_lock, open_session(self.sessionmaker) as session:
                row = (
                    await session.execute(
                        select(MeterSpend).where(
                            MeterSpend.agent == agent,
                            MeterSpend.kind == KIND_TASK,
                            MeterSpend.task_id == task_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    now = datetime.now(UTC)
                    row.closed_at = now
                    row.updated_at = now
                    log.info(
                        "meter.task_spend",
                        agent=agent,
                        task_id=task_id,
                        root_id=row.root_id,
                        calls=row.calls,
                        refused=row.refused_calls,
                        input=row.input_tokens,
                        output=row.output_tokens,
                        cache_write=row.cache_write_tokens,
                        cache_read=row.cache_read_tokens,
                    )
        except Exception:
            log.exception("meter.close_failed", agent=agent, task_id=task_id)

    async def report(self, agent: str, task_id: str, reported: dict[str, Any]) -> None:
        """Keep the agent's own usage report next to the measured numbers."""
        charge = Charge(agent, KIND_TASK, task_id)
        async with self._write_lock, open_session(self.sessionmaker) as session:
            row = await self._row(session, charge)
            row.reported = reported
            row.updated_at = datetime.now(UTC)
