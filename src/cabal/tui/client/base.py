from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


class BrokerError(RuntimeError):
    """Auth/dispatch failure surfaced to the UI (raised before the first delta)."""


@runtime_checkable
class BrokerClient(Protocol):
    default_persona: str

    async def __aenter__(self) -> BrokerClient: ...
    async def __aexit__(self, *exc: object) -> None: ...

    def send_message(self, persona: str, text: str) -> AsyncIterator[str]:
        """Dispatch `text` to `persona`; return an async iterator of response text
        deltas, in order. Callers use: `async for delta in client.send_message(p, t): ...`
        Raises BrokerError on auth/dispatch failure before the first delta.
        """
        ...
