"""Local-handler interface.

In solo mode each agent (Rook, Architect, ...) registers an instance with
the dispatch router at startup. In scale mode only the agent named via
`--agent` registers; everything else is remote and the router falls back
to HTTP.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cabal.core.a2a.types import Task


@runtime_checkable
class LocalHandler(Protocol):
    """Per-agent in-process handler contract."""

    name: str

    async def handle_task(self, task: Task) -> Task:
        """Process an inbound A2A Task and return a completed Task."""
        ...
