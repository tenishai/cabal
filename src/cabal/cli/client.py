"""HTTP client used by the cbl CLI to talk to a running Cabal Core daemon."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:7400"
DEFAULT_TOKEN_PATH = Path.home() / ".cabal" / "token"


def _default_token_path() -> Path:
    """Resolve DEFAULT_TOKEN_PATH at call time so tests can monkeypatch it."""
    import cabal.cli.client as _self

    return _self.DEFAULT_TOKEN_PATH


@dataclass
class TokenStore:
    path: Path = field(default_factory=_default_token_path)

    def save(self, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(token.strip())
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        return self.path.read_text().strip() or None


def base_url() -> str:
    return os.environ.get("CABAL_URL", DEFAULT_BASE_URL)


def make_client() -> httpx.Client:
    return httpx.Client(base_url=base_url(), timeout=httpx.Timeout(30.0))


def bootstrap(code: str) -> dict[str, Any]:
    with make_client() as c:
        resp = c.post("/auth/bootstrap", json={"code": code})
    resp.raise_for_status()
    return resp.json()


def list_agents(*, token: str) -> dict[str, Any]:
    with make_client() as c:
        resp = c.get("/admin/agents", headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()


def primus_rotate(*, token: str, persona: str, hard: bool = False) -> dict[str, Any]:
    with make_client() as c:
        resp = c.post(
            "/admin/primus/rotate",
            json={"persona": persona, "hard": hard},
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def dispatch(
    *, token: str, target: str, message_text: str, task_id: str | None = None
) -> dict[str, Any]:
    import uuid

    payload = {
        "target": target,
        "task": {
            "id": task_id or f"t-{uuid.uuid4().hex[:12]}",
            "status": {
                "state": "submitted",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": message_text}],
                },
            },
        },
    }
    with make_client() as c:
        resp = c.post(
            "/a2a/tasks",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()
