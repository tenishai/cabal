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


def reload_identity(*, token: str) -> dict[str, Any]:
    with make_client() as c:
        resp = c.post(
            "/admin/reload/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
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


def list_conversations(
    *,
    token: str,
    limit: int = 50,
    state: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if state is not None:
        params["state"] = state
    if target is not None:
        params["target"] = target
    with make_client() as c:
        resp = c.get(
            "/admin/conversations",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def get_conversation(*, token: str, task_id: str) -> dict[str, Any]:
    with make_client() as c:
        resp = c.get(
            f"/admin/conversations/{task_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def tail_events(*, token: str):
    """Generator yielding (event_name, payload_dict) tuples from
    /admin/events/stream. Runs forever until the connection drops."""
    import json

    with make_client() as c:
        with c.stream(
            "GET",
            "/admin/events/stream",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            resp.raise_for_status()
            event_name: str | None = None
            data_lines: list[str] = []
            for raw in resp.iter_lines():
                line = raw if isinstance(raw, str) else raw.decode("utf-8")
                if line == "":
                    if event_name is not None:
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except Exception:  # noqa: BLE001
                            payload = {"raw": "\n".join(data_lines)}
                        yield (event_name, payload)
                    event_name = None
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())


def mint_service_token(
    *,
    token: str,
    sub: str,
    scope: list[str],
    aud: str,
    ttl_days: int,
) -> dict[str, Any]:
    with make_client() as c:
        resp = c.post(
            "/admin/token/mint-service",
            json={"sub": sub, "scope": scope, "aud": aud, "ttl_days": ttl_days},
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def stream_dispatch(
    *,
    token: str,
    target: str,
    message_text: str,
):
    """Open an SSE stream against /agents/{target}/stream.

    Yields (event_name, payload_dict) tuples. Last event with final=True
    in result.status is the terminal frame.

    Synchronous — runs httpx.Client.stream() and parses SSE inline.
    """
    import json
    import uuid

    body = {
        "jsonrpc": "2.0",
        "id": f"req-{uuid.uuid4().hex[:8]}",
        "method": "message/stream",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message_text}],
            }
        },
    }
    with make_client() as c:
        with c.stream(
            "POST",
            f"/agents/{target}/stream",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            resp.raise_for_status()
            event_name: str | None = None
            data_lines: list[str] = []
            for raw in resp.iter_lines():
                line = raw if isinstance(raw, str) else raw.decode("utf-8")
                if line == "":
                    if event_name is not None:
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except Exception:  # noqa: BLE001
                            payload = {"raw": "\n".join(data_lines)}
                        yield (event_name, payload)
                    event_name = None
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())


def dispatch(
    *,
    token: str,
    target: str,
    message_text: str,
    task_id: str | None = None,
    history: list[dict[str, Any]] | None = None,
    context_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import uuid

    task: dict[str, Any] = {
        "id": task_id or f"t-{uuid.uuid4().hex[:12]}",
        "status": {
            "state": "submitted",
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message_text}],
            },
        },
    }
    if history:
        task["history"] = history
    if context_id:
        task["contextId"] = context_id
    if metadata:
        task["metadata"] = metadata

    payload = {"target": target, "task": task}
    with make_client() as c:
        resp = c.post(
            "/a2a/tasks",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()
