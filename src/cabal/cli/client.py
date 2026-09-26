"""HTTP client used by the cbl CLI to talk to a running Cabal Core daemon."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cabal.config.home import cabal_home
from cabal.core.dispatch_timeout import cli_dispatch_timeout

DEFAULT_BASE_URL = "http://127.0.0.1:7400"
#: An override for the token file; None means ``cabal_home()/token``. Tests
#: monkeypatch it.
DEFAULT_TOKEN_PATH: Path | None = None


def _default_token_path() -> Path:
    """The operator token file: ``$CABAL_HOME/token``, else ``~/.cabal/token``.

    FOLLOWS CABAL_HOME LIKE THE REST OF CABAL'S STATE (cabal#216). It was
    pinned to ``~/.cabal/token``, so two cabals under different homes shared
    one operator token and ``cbl reset-auth`` cleared the wrong one. With
    CABAL_HOME unset the path is unchanged. Resolved at call time, like
    ``cabal_home()`` itself, so the env var and the override both take effect
    without a re-import.
    """
    import cabal.cli.client as _self

    return _self.DEFAULT_TOKEN_PATH or cabal_home() / "token"


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


def make_client(*, timeout: httpx.Timeout | None = None) -> httpx.Client:
    """HTTP client for the daemon.

    The default 30s ceiling is deliberately SHORT and stays that way for most
    admin calls: a hung daemon should not make `cbl agents list` sit for
    minutes. The session-presence trio is tighter still -- see
    SESSION_TIMEOUT_S below.
    Routes that trigger a FEDERATED DISPATCH pass `cli_dispatch_timeout()`
    instead — the daemon's own ceiling is tunable via CABAL_DISPATCH_TIMEOUT_S,
    and a client pinned to 30s would abort an exchange the daemon completes.

    KEYWORD-ONLY, and that is a merge decision rather than a style one. This PR
    changed the parameter from `timeout_s: float` to an `httpx.Timeout`; main
    meanwhile added SESSION_TIMEOUT_S below and three POSITIONAL callers using
    it. Keyword-only means those callers cannot silently pass a float where a
    Timeout is expected — they are converted explicitly instead.
    """
    return httpx.Client(
        base_url=base_url(), timeout=timeout or httpx.Timeout(30.0)
    )


#: Presence calls run from a Stop hook on every turn, so their cost is paid by
#: the operator's latency, not by a batch job. 30s is right for a dispatch and
#: wrong here: a firewall that DROPs rather than refuses would stall the turn.
#: `beat` can make two of these back to back (heartbeat, then register on
#: 404), so this bounds that pair at 10s rather than 60s (PR #148 review nit).
SESSION_TIMEOUT_S = 5.0


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


def register_session(
    *, token: str, name: str, work: dict[str, Any] | None = None
) -> dict[str, Any]:
    with make_client(timeout=httpx.Timeout(SESSION_TIMEOUT_S)) as c:
        resp = c.post(
            "/admin/sessions/register",
            json={"name": name, "work": work},
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def heartbeat_session(
    *, token: str, name: str, work: dict[str, Any] | None = None
) -> dict[str, Any]:
    with make_client(timeout=httpx.Timeout(SESSION_TIMEOUT_S)) as c:
        resp = c.post(
            f"/admin/sessions/{name}/heartbeat",
            json={"work": work},
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def end_session(*, token: str, name: str) -> dict[str, Any]:
    with make_client(timeout=httpx.Timeout(SESSION_TIMEOUT_S)) as c:
        resp = c.post(
            f"/admin/sessions/{name}/end",
            headers={"Authorization": f"Bearer {token}"},
        )
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
    persona: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"sub": sub, "scope": scope, "aud": aud, "ttl_days": ttl_days}
    if persona is not None:
        body["persona"] = persona
    with make_client() as c:
        resp = c.post(
            "/admin/token/mint-service",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def persona_graduate(
    *,
    token: str,
    name: str,
    endpoint: str,
    audience: str,
    agent_card_json: dict | None = None,
    agent_card_kid: str = "",
) -> dict[str, Any]:
    """Register a persona as a federated sovereign peer (cabal#45)."""
    body: dict[str, Any] = {
        "endpoint": endpoint,
        "audience": audience,
        "agent_card_json": agent_card_json or {},
        "agent_card_kid": agent_card_kid,
    }
    with make_client() as c:
        resp = c.post(
            f"/admin/persona/{name}/graduate",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def persona_absorb(*, token: str, name: str) -> dict[str, Any]:
    """Re-absorb a federated persona back into this cabal as a local member."""
    with make_client() as c:
        resp = c.post(
            f"/admin/persona/{name}/absorb",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def secret_set(
    *,
    token: str,
    persona: str,
    handler: str,
    key: str,
    value: str,
    purpose: str | None = None,
) -> dict[str, Any]:
    with make_client() as c:
        resp = c.post(
            f"/admin/secrets/{persona}/{handler}",
            json={"key": key, "value": value, "purpose": purpose},
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def secret_unset(
    *, token: str, persona: str, handler: str, key: str
) -> dict[str, Any]:
    with make_client() as c:
        resp = c.delete(
            f"/admin/secrets/{persona}/{handler}/{key}",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def secret_list(
    *,
    token: str,
    persona: str | None = None,
    handler: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if persona is not None:
        params["persona"] = persona
    if handler is not None:
        params["handler"] = handler
    with make_client() as c:
        resp = c.get(
            "/admin/secrets",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def stream_dispatch(
    *,
    token: str,
    target: str,
    message_text: str,
    context_id: str | None = None,
):
    """Open an SSE stream against /agents/{target}/stream.

    Yields (event_name, payload_dict) tuples. Last event with final=True
    in result.status is the terminal frame.

    Synchronous — runs httpx.Client.stream() and parses SSE inline.

    ``context_id`` is forwarded as ``contextId`` in the A2A params when
    provided; the server threads it onto the inbound Task so the
    DoomLoopGuardrail can group chat-session hops by stable session key
    (cabal#59).
    """
    import json
    import uuid

    params: dict = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": message_text}],
        },
    }
    if context_id is not None:
        params["contextId"] = context_id

    body = {
        "jsonrpc": "2.0",
        "id": f"req-{uuid.uuid4().hex[:8]}",
        "method": "message/stream",
        "params": params,
    }
    with make_client(timeout=cli_dispatch_timeout()) as c:
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
    with make_client(timeout=cli_dispatch_timeout()) as c:
        resp = c.post(
            "/a2a/tasks",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()
