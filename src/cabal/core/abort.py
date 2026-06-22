"""AbortGraph + AbortCoordinator — transitive cancellation across nested dispatch.

cabal#11. Models the live tree of in-flight invocations (persona dispatch,
handler call, tool call, LLM stream) as a graph: nodes are invocations, edges
are parent→child. Cancelling a node propagates transitively to every
descendant — no per-layer signal chaining required.

Cooperative, in-process design
------------------------------

Cancellation is *cooperative*, not preemptive: each node owns an
``asyncio.Event``. ``abort(node)`` sets that event on the node and every
descendant. Running code observes the abort in two ways:

  - **At safe boundaries** — call :meth:`AbortCoordinator.raise_if_aborted`
    before an LLM call, before a tool call, between loop iterations. The
    checkpoint written just before each LLM call (cabal#2) makes the boundary
    a clean resume point: abort there, and ``resume_local`` picks up from the
    same checkpoint.
  - **Mid-await** — wrap a long-running awaitable in :meth:`AbortCoordinator.guard`
    so an in-flight LLM/tool call is abandoned promptly rather than waiting for
    the next boundary.

The graph lives in process only — no DB table. Parentage is already
expressible from ``task.metadata['cabal_parent_task']`` and the hierarchical
task IDs, and "what's running right now?" is a :meth:`dump` of live state, not
something to persist. All mutations are synchronous (no awaits), so on a single
event loop they are atomic with respect to each other — no lock needed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "AbortCoordinator",
    "AbortNode",
    "TaskAborted",
]

T = TypeVar("T")

# Conventional node kinds (free-form str — these are the ones cabal wires today).
KIND_DISPATCH = "dispatch"
KIND_SUBORDINATE = "subordinate"
KIND_LLM = "llm"
KIND_TOOL = "tool"


class TaskAborted(Exception):
    """Raised by a cooperative abort check when a node has been aborted.

    Carries the abort ``reason`` so the dispatch layer can surface it (e.g. as
    a cabal reason code) and so a checkpointed task can record *why* it stopped.
    """

    def __init__(self, node_id: str, reason: str) -> None:
        self.node_id = node_id
        self.reason = reason
        super().__init__(f"task {node_id!r} aborted: {reason}")


@dataclass(slots=True)
class AbortNode:
    """One in-flight invocation in the AbortGraph.

    ``event`` is set when this node (or an ancestor) is aborted; cooperative
    checks key off it. ``children`` holds direct child node IDs only — the full
    descendant set is walked on demand.
    """

    node_id: str
    kind: str
    parent_id: str | None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    children: set[str] = field(default_factory=set)
    reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def aborted(self) -> bool:
        return self.event.is_set()


class AbortCoordinator:
    """Owns the AbortGraph and routes cancellation through it.

    One instance per cabal-core process, created at app lifespan and shared by
    the dispatch router, personas, and the admin surface.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, AbortNode] = {}

    # -- lifecycle -------------------------------------------------------

    def register(
        self, node_id: str, *, parent_id: str | None = None, kind: str = KIND_DISPATCH
    ) -> AbortNode:
        """Add a node to the graph and link it under ``parent_id``.

        Idempotent on ``node_id`` — re-registering returns the existing node
        (a task that re-enters dispatch keeps its place). If the parent is
        already aborted, the new child inherits the abort immediately, closing
        the race where a child registers just after its parent was cancelled.
        """
        existing = self._nodes.get(node_id)
        if existing is not None:
            return existing

        node = AbortNode(node_id=node_id, kind=kind, parent_id=parent_id)
        self._nodes[node_id] = node

        parent = self._nodes.get(parent_id) if parent_id else None
        if parent is not None:
            parent.children.add(node_id)
            if parent.aborted:
                # Inherit a cancellation that already happened.
                node.reason = parent.reason
                node.event.set()
        log.debug("abort.register", node_id=node_id, parent_id=parent_id, kind=kind)
        return node

    def complete(self, node_id: str) -> None:
        """Remove a node on normal completion. Unlinks it from its parent.

        Does not touch the abort state of anything else — completion is the
        ordinary end of an invocation, distinct from cancellation.
        """
        node = self._nodes.pop(node_id, None)
        if node is None:
            return
        parent = self._nodes.get(node.parent_id) if node.parent_id else None
        if parent is not None:
            parent.children.discard(node_id)
        # Re-parent any still-live children to this node's parent so the graph
        # stays connected if a parent completes before a child (shouldn't happen
        # in well-formed flows, but keeps `dump` honest).
        for child_id in node.children:
            child = self._nodes.get(child_id)
            if child is not None:
                child.parent_id = node.parent_id
                if parent is not None:
                    parent.children.add(child_id)
        log.debug("abort.complete", node_id=node_id)

    # -- cancellation ----------------------------------------------------

    def abort(self, node_id: str, reason: str = "aborted") -> int:
        """Abort ``node_id`` and every descendant. Returns the number of nodes
        signalled (0 if the node is unknown). Idempotent — already-set events
        stay set, and a node only takes the first reason recorded against it.
        """
        root = self._nodes.get(node_id)
        if root is None:
            return 0
        signalled = 0
        for node in self._walk(root):
            if not node.event.is_set():
                node.reason = reason
                node.event.set()
                signalled += 1
        log.info("abort.fire", node_id=node_id, reason=reason, signalled=signalled)
        return signalled

    def is_aborted(self, node_id: str) -> bool:
        node = self._nodes.get(node_id)
        return node is not None and node.aborted

    def raise_if_aborted(self, node_id: str) -> None:
        """Cooperative checkpoint — raise :class:`TaskAborted` if aborted.

        Call at safe boundaries (before each LLM/tool call, between loop
        iterations). A node not in the graph is treated as not-aborted.
        """
        node = self._nodes.get(node_id)
        if node is not None and node.aborted:
            raise TaskAborted(node_id, node.reason or "aborted")

    async def guard(self, node_id: str, coro: Awaitable[T]) -> T:
        """Await ``coro``, abandoning it promptly if ``node_id`` is aborted.

        Races the awaitable against the node's abort event. If the abort wins,
        the in-flight awaitable is cancelled and :class:`TaskAborted` is raised.
        An untracked node degrades to a plain ``await``.
        """
        node = self._nodes.get(node_id)
        if node is None:
            return await coro
        if node.aborted:
            # Don't even start the work if we're already cancelled.
            with contextlib.suppress(Exception):
                # coro may be a bare coroutine that must be closed to avoid a
                # "never awaited" warning.
                getattr(coro, "close", lambda: None)()
            raise TaskAborted(node_id, node.reason or "aborted")

        work = asyncio.ensure_future(coro)
        abort_wait = asyncio.ensure_future(node.event.wait())
        try:
            await asyncio.wait(
                {work, abort_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if work.done():
                return work.result()
            # Abort fired first — cancel the in-flight work and surface it.
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
            raise TaskAborted(node_id, node.reason or "aborted")
        finally:
            if not abort_wait.done():
                abort_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await abort_wait

    # -- observability ---------------------------------------------------

    def dump(self, node_id: str | None = None) -> dict | list[dict]:
        """Serialize the live tree.

        With ``node_id`` → that subtree as a nested dict. Without → a list of
        root subtrees (nodes with no live parent). Powers the
        ``GET /admin/tasks/{id}`` live-tree view.
        """
        if node_id is not None:
            node = self._nodes.get(node_id)
            return self._subtree(node) if node is not None else {}
        roots = [
            n
            for n in self._nodes.values()
            if n.parent_id is None or n.parent_id not in self._nodes
        ]
        return [self._subtree(n) for n in roots]

    # -- internals -------------------------------------------------------

    def _walk(self, root: AbortNode) -> list[AbortNode]:
        """Root + all descendants, cycle-safe."""
        seen: set[str] = set()
        order: list[AbortNode] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.node_id in seen:
                continue
            seen.add(node.node_id)
            order.append(node)
            for child_id in node.children:
                child = self._nodes.get(child_id)
                if child is not None:
                    stack.append(child)
        return order

    def _subtree(self, node: AbortNode) -> dict:
        return {
            "node_id": node.node_id,
            "kind": node.kind,
            "parent_id": node.parent_id,
            "aborted": node.aborted,
            "reason": node.reason,
            "created_at": node.created_at.isoformat(),
            "children": [
                self._subtree(self._nodes[c])
                for c in sorted(node.children)
                if c in self._nodes
            ],
        }
