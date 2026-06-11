"""Per-Task session tracking.

Each persona dispatch is wrapped in a session: start at handle_task entry,
end at handle_task return. The session_id is bound to the Task.id, so
techne treats every Cabal Task as a captured working session — which
then feeds into thread detection + Archivist recall via the same DB.

The Protocol stays narrow so we can swap in OTLP / graphiti / a no-op
stub without rewriting the persona layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

log = structlog.get_logger(__name__)


@runtime_checkable
class SessionTracker(Protocol):
    """Lifecycle hooks for a per-Task session."""

    def start(self, *, external_id: str, agent_identity: str) -> str:
        """Begin a session. Return the session_id."""
        ...

    def end(self, *, session_id: str, summary: str) -> None:
        """Close a session with a final summary."""
        ...


class NoOpSessionTracker:
    """Default tracker — does nothing, returns the external_id as session_id."""

    def start(self, *, external_id: str, agent_identity: str) -> str:
        return external_id

    def end(self, *, session_id: str, summary: str) -> None:
        return None


@dataclass
class TechneSessionTracker:
    """techne-backed session tracker.

    Lazily imports `techne` and `techne.lifecycle` so the dep stays soft.
    All techne calls are wrapped in try/except — observability must not
    block dispatch.
    """

    db_path: Path
    data_dir: Path
    platform: str = "cabal"

    def _make_config(self) -> Any:
        from techne import Config

        return Config(
            db_path=self.db_path,
            data_dir=self.data_dir,
            platform=self.platform,
        )

    def start(self, *, external_id: str, agent_identity: str) -> str:
        try:
            from techne.lifecycle import init_session_for_external_id

            cfg = self._make_config()
            return init_session_for_external_id(
                cfg, external_id, agent_identity=agent_identity
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "session.start_failed",
                external_id=external_id,
                agent=agent_identity,
                error=str(exc),
            )
            return external_id

    def end(self, *, session_id: str, summary: str) -> None:
        try:
            from techne.lifecycle import run_session_end

            cfg = self._make_config()
            run_session_end(cfg, session_id, summary=summary)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "session.end_failed", session_id=session_id, error=str(exc)
            )
