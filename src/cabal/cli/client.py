"""HTTP client used by the cbl CLI to talk to a running Cabal Core daemon."""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import time
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
#: A saved operator token with less than this long left is renewed when it is
#: loaded (#395), so a long `cbl task --wait` or a set left running keeps its
#: operator session.
RENEW_WITHIN_S = 10 * 60

log = logging.getLogger(__name__)


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
        # Written to a 0600 temp file and renamed over: several `cbl` processes
        # may renew at once, and none may read a half-written token.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(token.strip())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def load(self, *, renew: bool = True) -> str | None:
        """The saved token. An operator token close to expiry is renewed
        first and the new one saved; if renewal fails the old token is
        returned, and the call it is used for reports why."""
        if not self.path.exists():
            return None
        token = self.path.read_text().strip() or None
        if token is None or not renew:
            return token
        left = _operator_seconds_left(token)
        if left is None or left <= 0 or left > RENEW_WITHIN_S:
            return token
        # Opportunistic, and on the per-turn presence hook's path, so held to
        # the presence calls' short timeout: a daemon that accepts and never
        # answers must not stall a turn for the 30s default (PR 397 review).
        try:
            fresh = renew_token(token=token, timeout=httpx.Timeout(SESSION_TIMEOUT_S))["access_token"]
            if not isinstance(fresh, str) or not fresh:
                raise ValueError("no access_token in the renewal response")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            log.debug("operator token renewal failed: %s", exc)
            return token
        try:
            self.save(fresh)
        except OSError as exc:  # e.g. a read-only CABAL_HOME: use it, unsaved
            log.debug("renewed operator token not saved: %s", exc)
        log.debug("operator token renewed (%ds were left)", left)
        return fresh


def _operator_seconds_left(token: str) -> int | None:
    """Seconds until an operator token expires, or None for any other token.
    Reads the payload without verifying it; the daemon verifies on renewal."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    if not isinstance(claims, dict) or "operator" not in (claims.get("cabal_scope") or []):
        return None
    exp = claims.get("exp")
    return int(exp - time.time()) if isinstance(exp, (int, float)) else None


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
#: In an operator token's last ten minutes each call's token load may also try a
#: renewal under this same timeout (#395), so the pair's worst case is 20s.
SESSION_TIMEOUT_S = 5.0


def bootstrap(code: str) -> dict[str, Any]:
    with make_client() as c:
        resp = c.post("/auth/bootstrap", json={"code": code})
    resp.raise_for_status()
    return resp.json()


def renew_token(*, token: str, timeout: httpx.Timeout | None = None) -> dict[str, Any]:
    """Swap a valid operator token for a fresh one (`POST /auth/renew`)."""
    with make_client(timeout=timeout) as c:
        resp = c.post("/auth/renew", headers={"Authorization": f"Bearer {token}"})
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


def running_tasks(*, token: str) -> dict[str, Any]:
    """GET /admin/tasks/running (cabal issue 405)."""
    with make_client() as c:
        resp = c.get("/admin/tasks/running", headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()


def start_drain(*, token: str, deadline_s: float | None = None) -> dict[str, Any]:
    """POST /admin/drain (cabal issue 405)."""
    with make_client() as c:
        resp = c.post(
            "/admin/drain",
            json={"deadline_s": deadline_s},
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


def cancel_drain(*, token: str) -> dict[str, Any]:
    """POST /admin/drain/cancel (cabal issue 405)."""
    with make_client() as c:
        resp = c.post("/admin/drain/cancel", headers={"Authorization": f"Bearer {token}"})
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
    allowed_handlers: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"sub": sub, "scope": scope, "aud": aud, "ttl_days": ttl_days}
    if persona is not None:
        body["persona"] = persona
    if allowed_handlers is not None:
        body["allowed_handlers"] = allowed_handlers
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


class A2AError(Exception):
    """A JSON-RPC error answered by an A2A v1.0 endpoint."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _a2a_v1(*, token: str, target: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """One A2A v1.0 JSON-RPC call to /agents/{target}/a2a; returns `result`.

    Short client timeout: SendMessage with returnImmediately and GetTask both
    answer from the database, so neither needs the federated-dispatch ceiling.
    """
    import uuid

    body = {"jsonrpc": "2.0", "id": f"req-{uuid.uuid4().hex[:8]}", "method": method, "params": params}
    with make_client() as c:
        resp = c.post(
            f"/agents/{target}/a2a",
            json=body,
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        )
    resp.raise_for_status()
    reply = resp.json()
    if "error" in reply:
        err = reply["error"]
        raise A2AError(err.get("code", 0), err.get("message", "unknown error"))
    result: dict[str, Any] = reply["result"]
    return result


def dispatch_detached(
    *,
    token: str,
    target: str,
    message_text: str,
    context_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SendMessage with returnImmediately (#277): returns the v1.0 Task, in
    `submitted`, once the daemon has authorised it and recorded it. The persona
    runs on, owned by the daemon; read it back with `get_task`."""
    import uuid

    message: dict[str, Any] = {
        "messageId": f"m-{uuid.uuid4().hex[:12]}",
        "role": "ROLE_USER",
        "parts": [{"text": message_text}],
    }
    if context_id:
        message["contextId"] = context_id
    params: dict[str, Any] = {"message": message, "configuration": {"returnImmediately": True}}
    if metadata:
        # The request's metadata, which SendMessage makes the task's (routes/a2a_v1.py).
        params["metadata"] = metadata
    result = _a2a_v1(token=token, target=target, method="SendMessage", params=params)
    task: dict[str, Any] = result["task"]
    return task


def answer_task(*, token: str, target: str, task_id: str, text: str) -> dict[str, Any]:
    """SendMessage naming a paused task: for a group task that ended with
    questions, the answer puts it back in the group's queue (cabal issue 423)."""
    import uuid

    message = {
        "messageId": f"m-{uuid.uuid4().hex[:12]}",
        "role": "ROLE_USER",
        "taskId": task_id,
        "parts": [{"text": text}],
    }
    result = _a2a_v1(token=token, target=target, method="SendMessage", params={"message": message})
    task: dict[str, Any] = result["task"]
    return task


def cancel_task(*, token: str, target: str, task_id: str) -> dict[str, Any]:
    """CancelTask: a scheduled task is canceled before it starts, a running one
    through the abort graph (cabal issues 406, #277)."""
    return _a2a_v1(token=token, target=target, method="CancelTask", params={"id": task_id})


def groups(*, token: str) -> dict[str, list[str]]:
    """GET /admin/groups (cabal issue 423): name -> members."""
    body = _admin_json("GET", "/admin/groups", token=token)
    return dict(body.get("groups") or {})


def scheduled_tasks(*, token: str) -> dict[str, Any]:
    """GET /admin/tasks/scheduled (cabal issue 406)."""
    with make_client() as c:
        resp = c.get("/admin/tasks/scheduled", headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()


def _admin_json(method: str, path: str, *, token: str, **kw: Any) -> dict[str, Any]:
    with make_client() as c:
        resp = c.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    return body


def review_start(
    *, token: str, repo: str, pr: int, sha: str, seats: list[str] | None = None
) -> dict[str, Any]:
    """POST /reviews (cabal issue 399): queue a review of one PR head."""
    payload: dict[str, Any] = {"repo": repo, "pr": pr, "sha": sha}
    if seats:
        payload["seats"] = seats
    return _admin_json("POST", "/reviews", token=token, json=payload)


def review_list(*, token: str, state: str | None = None, limit: int = 100) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if state:
        params["state"] = state
    return _admin_json("GET", "/reviews", token=token, params=params)


def review_show(*, token: str, run_id: str) -> dict[str, Any]:
    return _admin_json("GET", f"/reviews/{run_id}", token=token)


def review_cancel(*, token: str, run_id: str, reason: str | None = None) -> dict[str, Any]:
    payload = {"reason": reason} if reason else None
    return _admin_json("POST", f"/reviews/{run_id}/cancel", token=token, json=payload)


def get_task(*, token: str, target: str, task_id: str) -> dict[str, Any]:
    """GetTask: the v1.0 Task as the daemon has it now."""
    return _a2a_v1(token=token, target=target, method="GetTask", params={"id": task_id})


def task_target(*, token: str, task_id: str) -> str:
    """The persona a task was sent to. GetTask is served per persona, so a
    caller that only has the id asks the operator's conversation view."""
    return str(get_conversation(token=token, task_id=task_id)["conversation"]["target_agent"])
