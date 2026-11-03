"""`cbl` CLI entry point.

Phase 1 surface (per cabal-handoff.md §4):
    cbl daemon up        — launch Cabal Core (solo mode by default)
    cbl login            — trade a bootstrap code for an operator JWT
    cbl dispatch         — send a one-shot A2A Task and print the result
    cbl task <id>        — read a detached task (`dispatch --detach`), or --wait for it
    cbl version          — print version
    cbl guide [name]     — list or print the guides bundled in the package
    cbl token mint       — (Phase 2) mint a session_token
    cbl reload identity  — (Phase 2) hot-reload identity.toml
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer

from cabal import __version__
from cabal.cli import client as api

app = typer.Typer(
    name="cbl",
    help="Cabal control-plane CLI.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


daemon_app = typer.Typer(help="Manage the local Cabal Core daemon.")
app.add_typer(daemon_app, name="daemon")


@app.command("version")
def version_cmd() -> None:
    """Print Cabal version and exit."""
    typer.echo(f"cabal {__version__}")


@app.command("guide")
def guide_cmd(
    name: str = typer.Argument(None, help="Guide to print. Omit to list them."),
) -> None:
    """Print a guide that ships with cabal, or list them.

    The guides are package data, so this works from an installed wheel with no
    repo checkout. Output is raw markdown, meant for an agent to read.
    """
    from cabal.guides import MANIFEST, guide_text

    if not name:
        typer.echo("Bundled guides (cbl guide <name>):\n")
        width = max(len(n) for n in MANIFEST)
        for n, desc in MANIFEST.items():
            typer.echo(f"  {n:<{width}}  {desc}")
        return
    try:
        typer.echo(guide_text(name), nl=False)
    except KeyError:
        typer.echo(
            f"no bundled guide named {name!r}; valid names: {', '.join(MANIFEST)}",
            err=True,
        )
        raise typer.Exit(code=2) from None


@daemon_app.command("up")
def daemon_up(
    agent: str = typer.Option(
        "all",
        "--agent",
        help="'all' for solo mode (every persona local), or a single agent name "
        "(e.g. 'mason-primus') for scale mode.",
    ),
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(7400, help="Bind port."),
    config: str = typer.Option(
        "",
        help=(
            "Path to identity.toml. Default: ./identity.toml when the working "
            "directory holds cabal state (identity.toml, data/cabal.db or "
            "data/.master_key), else $CABAL_HOME/identity.toml (~/.cabal)."
        ),
    ),
) -> None:
    """Launch Cabal Core."""
    import uvicorn

    from cabal.config.home import resolve_identity_path
    from cabal.core.app import create_app

    fastapi_app = create_app(agent=agent, config_path=str(resolve_identity_path(config)))
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


def _auth_hint(detail: str) -> str:
    """Append the remedy when a failure is really a stale operator token.

    A token signed by a rotated (or reset) signing key comes back as
    `No key for kid` — accurate, and useless to anyone who does not know
    cabal's crypto model. cabal#92 Mode B: the token does not self-heal and
    nothing tells you to re-authenticate.
    """
    if "No key for kid" in detail or "invalid_key_id" in detail:
        return (
            f"{detail}\n"
            "  → your saved token was signed by a signing key that no longer "
            "exists (rotation, reset-auth, or a fresh database).\n"
            "  → re-authenticate: `cbl login --code <CODE>` using the code in "
            "the daemon's startup log."
        )
    return detail


@app.command("reset-auth")
def reset_auth_cmd(
    purge_secrets: bool = typer.Option(
        False,
        "--purge-secrets",
        help="ALSO delete every stored secret. They are encrypted with the "
        "master key you no longer have, so this is unrecoverable.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    db_url: str = typer.Option(
        "", "--db-url", help="Override the database URL (default: CABAL_DB_URL, else data/cabal.db under the state root)."
    ),
) -> None:
    """Recover from a lost or changed CABAL_MASTER_KEY (cabal#92).

    Backs the database up, clears the signing keys cabal can no longer decrypt,
    and drops the cached operator token that the deleted key signed. The next
    `cbl daemon up` mints a fresh key and a new bootstrap code, delivered per
    `cabal.bootstrap_code` (#325).

    Secrets are LEFT IN PLACE unless --purge-secrets: they are the one thing
    here that cannot be regenerated.
    """
    from cabal.core.auth.reset import ResetError, reset_auth, sqlite_path_from_url
    from cabal.core.db import effective_db_url

    url = effective_db_url(db_url or None)
    try:
        path = sqlite_path_from_url(url)
    except ResetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"database:  {path}")
    typer.echo(f"clears:    signing_keys, {api.TokenStore().path}")
    if purge_secrets:
        typer.echo("ALSO clears: secrets — UNRECOVERABLE")
    else:
        typer.echo("keeps:     secrets (use --purge-secrets to drop them too)")

    if not yes:
        typer.confirm("Proceed?", abort=True)

    try:
        report = reset_auth(
            url, purge_secrets=purge_secrets, token_path=api.TokenStore().path
        )
    except ResetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"backup:            {report.backup_path}")
    typer.echo(f"signing keys gone: {report.signing_keys_deleted}")
    if report.secrets_purged:
        typer.echo(f"secrets purged:    {report.secrets_purged}")
    if report.secrets_remaining:
        typer.echo(
            f"secrets kept:      {report.secrets_remaining} "
            "(still encrypted with the old key — undecryptable until it is restored)"
        )
    if report.token_removed:
        typer.echo("operator token:    removed")
    typer.echo(
        "next: `cbl daemon up`, then `cbl login --code <CODE>`. The code is in the "
        "daemon log (bootstrap_code = \"log\", the default) or in "
        "$CABAL_HOME/bootstrap_code (\"file\"). With \"off\" no code is minted: "
        "start once with CABAL_BOOTSTRAP_CODE=file."
    )


@app.command("login")
def login(
    code: str = typer.Option(
        None,
        "--code",
        prompt="Bootstrap code",
        help="6-character code the daemon mints at startup: in its log, or in $CABAL_HOME/bootstrap_code with bootstrap_code = \"file\".",
    ),
) -> None:
    """Trade a bootstrap code for an operator JWT (saved to $CABAL_HOME/token, default ~/.cabal/token)."""
    try:
        body = api.bootstrap(code.strip().upper())
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"login failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    store = api.TokenStore()
    store.save(body["access_token"])
    persona = body.get("persona")
    typer.echo(
        f"Logged in as {'operator' if persona is None else 'persona ' + persona}. "
        f"Token saved to {store.path}."
    )


@app.command("dispatch")
def dispatch(
    target: str = typer.Argument(..., help="Target agent name (e.g. 'echo')."),
    message: str = typer.Argument(..., help="Initial user message."),
    raw: bool = typer.Option(False, "--raw", help="Print full Task JSON."),
    context: str | None = typer.Option(
        None,
        "--context",
        help=(
            "A2A contextId to send the task under. One-shots sharing an id "
            "share a per-conversation delegation budget instead of the "
            "caller-wide one."
        ),
    ),
    detach: bool = typer.Option(
        False,
        "--detach",
        help=(
            "Return at once with the task id (A2A returnImmediately) instead of "
            "waiting for the answer; read it with `cbl task <id>`. The daemon "
            "runs the task, so it survives this client. A federated peer's call "
            "still ends at the daemon's CABAL_DISPATCH_TIMEOUT_S, so raise that "
            "(on the daemon) for a peer task that runs longer than 30s."
        ),
    ),
) -> None:
    """Send a one-shot A2A Task and print the response."""
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        if detach:
            task = api.dispatch_detached(
                token=token, target=target, message_text=message, context_id=context
            )
            if raw:
                typer.echo(json.dumps(task, indent=2))
            else:
                typer.echo(f"{task['id']} [{_state_name(task)}]")
            return
        response = api.dispatch(
            token=token, target=target, message_text=message, context_id=context
        )
    except api.A2AError as exc:
        typer.echo(f"dispatch failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"dispatch failed: {_auth_hint(detail)}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if raw:
        typer.echo(json.dumps(response, indent=2))
        return
    state = response["status"]["state"]
    text_parts = [
        p["text"]
        for p in response["status"].get("message", {}).get("parts", [])
        if p.get("kind") == "text"
    ]
    typer.echo(f"[{state}] {' '.join(text_parts)}")


# v1.0 states after which nothing more happens without the caller: the task is
# done, or paused waiting on a caller `cbl task` cannot answer.
_TASK_STOP_STATES = frozenset(
    {"completed", "failed", "canceled", "rejected", "input-required", "auth-required"}
)
EXIT_TASK_TIMEOUT = 3


def _state_name(task: dict[str, Any]) -> str:
    """`TASK_STATE_INPUT_REQUIRED` -> `input-required`."""
    return str(task["status"]["state"]).removeprefix("TASK_STATE_").lower().replace("_", "-")


@app.command("task")
def task_cmd(
    task_id: str = typer.Argument(..., help="Task id printed by `cbl dispatch --detach`."),
    raw: bool = typer.Option(False, "--raw", help="Print the full Task JSON."),
    wait: bool = typer.Option(
        False, "--wait", help="Poll until the task ends. Exit 1 unless it completed."
    ),
    interval: float = typer.Option(2.0, "--interval", min=0.05, help="Seconds between polls."),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        min=0.0,
        help=f"With --wait, give up after this many seconds (exit {EXIT_TASK_TIMEOUT}).",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help="Persona the task was sent to. Default: looked up (operator token).",
    ),
) -> None:
    """Show a task's state and, once it has ended, its message."""
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        if target is None:
            target = api.task_target(token=token, task_id=task_id)
        while True:
            try:
                task = api.get_task(token=token, target=target, task_id=task_id)
            except httpx.RequestError as exc:
                # A restart or blip is what --detach exists to survive: keep waiting.
                if not wait:
                    raise
                typer.echo(f"daemon unreachable ({exc}); retrying", err=True)
                task = None
            if task is not None and (not wait or _state_name(task) in _TASK_STOP_STATES):
                break
            if deadline is not None and time.monotonic() + interval > deadline:
                state = _state_name(task) if task is not None else "unknown"
                typer.echo(f"timed out after {timeout:g}s; task {task_id} is {state}", err=True)
                raise typer.Exit(code=EXIT_TASK_TIMEOUT)
            time.sleep(interval)
    except api.A2AError as exc:
        typer.echo(f"task failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"task failed: {_auth_hint(detail)}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    state = _state_name(task)
    if raw:
        typer.echo(json.dumps(task, indent=2))
    else:
        parts = task["status"].get("message", {}).get("parts", [])
        text = " ".join(p["text"] for p in parts if p.get("text"))
        typer.echo(f"[{state}] {text}".rstrip())
    if wait and state != "completed":
        raise typer.Exit(code=1)


@app.command("chat")
def chat(
    target: str = typer.Argument(..., help="Persona to chat with (e.g. 'archivist')."),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="SSE streaming (default true)."),
    edit_format: str = typer.Option(
        "none",
        "--edit-format",
        help=(
            "Output format: 'none' (text, default) or 'search-replace' "
            "(parse + apply aider-style SEARCH/REPLACE blocks). Must "
            "match the persona's identity.toml edit_format."
        ),
    ),
    no_apply: bool = typer.Option(
        False,
        "--no-apply",
        help=(
            "Dry-run: render diffs but never touch disk. Useful for "
            "review without code-write trust."
        ),
    ),
    project_root: Path = typer.Option(
        Path.cwd(),
        "--project-root",
        help="Repo root that SEARCH/REPLACE block paths resolve against.",
    ),
) -> None:
    """Multi-turn chat with a persona. Empty line or :exit to quit."""
    import sys
    import uuid

    if edit_format not in {"none", "search-replace"}:
        typer.echo(
            f"unknown --edit-format {edit_format!r}; expected 'none' or 'search-replace'.",
            err=True,
        )
        raise typer.Exit(code=2)

    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)

    context_id = f"chat-{uuid.uuid4().hex[:12]}"
    history: list[dict] = []
    mode_note = "streaming" if stream else "single-shot"
    if edit_format != "none":
        mode_note += f", edit:{edit_format}"
        if no_apply:
            mode_note += ", dry-run"
    typer.echo(f"chat with {target} ({mode_note}, context: {context_id}). type :exit to quit.")
    typer.echo("─" * 60)

    # Buffer streaming when an edit_format is active — we need the full
    # response before we can parse blocks.
    buffer_stream = edit_format != "none"

    while True:
        try:
            user_text = typer.prompt(">", prompt_suffix=" ", show_default=False)
        except (EOFError, typer.Abort):
            typer.echo("\nbye.")
            return
        user_text = user_text.strip()
        if not user_text or user_text in {":exit", ":quit", ":q"}:
            typer.echo("bye.")
            return

        if stream:
            agent_text = _stream_chat_turn(
                target, token, user_text, buffered=buffer_stream, context_id=context_id
            )
            if agent_text is None:
                continue  # error already printed
        else:
            try:
                response = api.dispatch(
                    token=token,
                    target=target,
                    message_text=user_text,
                    history=history,
                    context_id=context_id,
                )
            except httpx.HTTPStatusError as exc:
                try:
                    detail = exc.response.json().get("detail", exc.response.text)
                except Exception:  # noqa: BLE001
                    detail = exc.response.text
                typer.echo(f"\nerror: {detail}", err=True)
                continue
            except httpx.RequestError as exc:
                typer.echo(f"\ncould not reach daemon: {exc}", err=True)
                return
            message = response.get("status", {}).get("message") or {}
            agent_text = "".join(
                part.get("text", "")
                for part in message.get("parts", [])
                if part.get("kind") == "text"
            )
            state = response.get("status", {}).get("state", "?")
            prefix = target if state == "completed" else f"{target} [{state}]"
            if edit_format == "none":
                typer.echo(f"{prefix}: {agent_text}")
            else:
                typer.echo(f"{prefix}:")

        if edit_format == "search-replace":
            _render_search_replace_turn(agent_text, root=project_root, no_apply=no_apply)

        history.append({"role": "user", "parts": [{"kind": "text", "text": user_text}]})
        history.append({"role": "agent", "parts": [{"kind": "text", "text": agent_text}]})
        typer.echo("")
        sys.stdout.flush()


def _render_search_replace_turn(agent_text: str, *, root: Path, no_apply: bool) -> None:
    """Parse SEARCH/REPLACE blocks from the agent's response, prompt the
    operator y/n for each, apply (unless --no-apply), and render the
    resulting diff. Falls through to printing raw text when no blocks
    are present (e.g. the assistant asked a clarifying question)."""
    from cabal.cli.edit_format import (
        apply_block,
        parse_search_replace_blocks,
        render_diff,
        render_failed_match,
    )

    blocks = parse_search_replace_blocks(agent_text)
    if not blocks:
        typer.echo(agent_text)
        return

    typer.echo(agent_text)
    typer.echo("")
    typer.echo(f"  parsed {len(blocks)} edit block(s).")

    for i, block in enumerate(blocks, 1):
        prefix = f"  [{i}/{len(blocks)}] {block.path}"
        if no_apply:
            typer.echo(f"{prefix} (dry-run, not applied)")
            continue
        try:
            confirm = typer.confirm(f"{prefix} — apply?", default=True)
        except typer.Abort:
            typer.echo("  aborted; remaining blocks skipped.")
            return
        if not confirm:
            typer.echo("  (skipped)")
            continue
        result = apply_block(block, root=root)
        if not result.applied:
            typer.echo(render_failed_match(result))
            continue
        diff = render_diff(block.path, root=root)
        if diff:
            typer.echo(diff)
        else:
            typer.echo(f"  applied (no diff output for {block.path}).")


def _stream_chat_turn(
    target: str,
    token: str,
    user_text: str,
    *,
    buffered: bool = False,
    context_id: str | None = None,
) -> str | None:
    """Run one streaming turn. When `buffered=False` (default), prints
    deltas as they arrive. When `buffered=True`, accumulates silently —
    the caller renders the assembled text (used for edit-format
    parsing, which can't run mid-stream). Returns the full assembled
    agent text or None on error.

    ``context_id`` is forwarded to the stream endpoint so the server can
    thread it onto the Task for doom-loop detection (cabal#59).
    """
    import sys

    if not buffered:
        typer.echo(f"{target}: ", nl=False)
        sys.stdout.flush()
    full_text = ""
    try:
        for event_name, payload in api.stream_dispatch(
            token=token, target=target, message_text=user_text, context_id=context_id
        ):
            if event_name == "chunk":
                delta = payload.get("delta", "")
                if not buffered:
                    typer.echo(delta, nl=False)
                    sys.stdout.flush()
                full_text += delta
            elif event_name == "error":
                err = payload.get("error") or {}
                typer.echo(f"\n[error] {err.get('message', 'unknown')}", err=True)
                return None
            elif event_name == "status-update":
                if not buffered:
                    typer.echo("")  # newline at end
                return full_text
    except httpx.HTTPStatusError as exc:
        typer.echo(f"\nerror: {exc.response.text}", err=True)
        return None
    except httpx.RequestError as exc:
        typer.echo(f"\ncould not reach daemon: {exc}", err=True)
        return None
    if not buffered:
        typer.echo("")
    return full_text


@app.command("status")
def status() -> None:
    """List registered agents and their last heartbeats."""
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        body = api.list_agents(token=token)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"status failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    agents = body.get("agents", [])
    if not agents:
        typer.echo("(no agents registered)")
        return
    header = (
        f"{'NAME':<22} {'GEN':>4} {'KIND':<10} {'ROLE':<10} "
        f"{'STATUS':<10} {'TIER':<8} HEARTBEAT"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for a in agents:
        hb = _heartbeat_cell(a.get("last_heartbeat"), kind=a.get("kind", "member"))
        typer.echo(
            f"{a['name']:<22} {a['generation']:>4} {a.get('kind', 'member'):<10} "
            f"{a['role']:<10} {a['status']:<10} {a['safety_tier']:<8} {hb}"
        )
        # cabal#142 — what a live session is doing does not fit a column, and it
        # is the whole reason the row exists. Indented under its own row.
        work = a.get("work") or {}
        if work:
            typer.echo(f"{'':<22} ↳ {_format_work(work)}")


#: A session heartbeats once per turn, so a gap this long means the session is
#: gone rather than thinking. Clean de-registration cannot be relied on: the
#: Stop hook fires per-turn, and a crashed or SIGKILLed session never runs it at
#: all -- so staleness, not `cbl session end`, is what makes a dead session read
#: as dead. Acting on it (reaping the row) is cabal#70; this only shows it.
SESSION_STALE_AFTER_S = 15 * 60


def _heartbeat_cell(raw: str | None, *, kind: str, now: datetime | None = None) -> str:
    """Render a heartbeat, flagging a session that has stopped reporting."""
    if not raw:
        return "—"
    if kind != "session":
        return raw
    try:
        seen = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # An unparseable timestamp is shown as-is rather than guessed at; a
        # rendering helper must not invent liveness it cannot determine.
        return raw
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if (current - seen).total_seconds() > SESSION_STALE_AFTER_S:
        return f"{raw}  STALE"
    return raw


def _format_work(work: dict[str, object]) -> str:
    """Render a session's work payload for one terminal line.

    `repo`, `branch` and `task` are rendered in that order when present; any
    other keys are appended as `k=v` so an experiment with the shape still shows
    up rather than being silently dropped.
    """
    known = [str(work[k]) for k in ("repo", "branch", "task") if work.get(k)]
    extra = [f"{k}={v}" for k, v in sorted(work.items()) if k not in {"repo", "branch", "task"}]
    return " · ".join(known + extra) or "—"


session_app = typer.Typer(help="Register this agent session's presence (cabal#142).")
app.add_typer(session_app, name="session")


def _session_token() -> str:
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    return token


def _work_payload(
    repo: str | None, branch: str | None, task: str | None
) -> dict[str, str] | None:
    work = {k: v for k, v in (("repo", repo), ("branch", branch), ("task", task)) if v}
    return work or None


@session_app.command("register")
def session_register(
    name: str = typer.Argument(..., help="A stable name for this session, e.g. poe2."),
    repo: str = typer.Option(None, "--repo"),
    branch: str = typer.Option(None, "--branch"),
    task: str = typer.Option(None, "--task", help="One line: what you are doing."),
) -> None:
    """Announce this session as present. Idempotent — safe to re-run."""
    body = api.register_session(
        token=_session_token(), name=name, work=_work_payload(repo, branch, task)
    )
    typer.echo(f"registered {body['name']} (kind={body['kind']})")


@session_app.command("heartbeat")
def session_heartbeat(
    name: str = typer.Argument(...),
    repo: str = typer.Option(None, "--repo"),
    branch: str = typer.Option(None, "--branch"),
    task: str = typer.Option(None, "--task"),
) -> None:
    """Refresh liveness, and update the work line when given one."""
    api.heartbeat_session(
        token=_session_token(), name=name, work=_work_payload(repo, branch, task)
    )
    typer.echo(f"heartbeat {name}")


@session_app.command("end")
def session_end(name: str = typer.Argument(...)) -> None:
    """De-register on clean shutdown."""
    api.end_session(token=_session_token(), name=name)
    typer.echo(f"ended {name}")


conv_app = typer.Typer(help="Browse past Task conversations.")
app.add_typer(conv_app, name="conversations")


@conv_app.command("list")
def conversations_list(
    limit: int = typer.Option(20, help="Max rows."),
    state: str | None = typer.Option(None, help="Filter by state."),
    target: str | None = typer.Option(None, help="Filter by target agent."),
) -> None:
    """Most recent conversations first, with a result snippet."""
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        body = api.list_conversations(token=token, limit=limit, state=state, target=target)
    except httpx.HTTPStatusError as exc:
        typer.echo(f"failed: {exc.response.text}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    rows = body.get("conversations", [])
    if not rows:
        typer.echo("(no conversations)")
        return
    header = f"{'TASK':<20} {'TARGET':<22} {'GEN':>4} {'STATE':<12} {'UPDATED':<20} SNIPPET"
    typer.echo(header)
    typer.echo("-" * 110)
    for r in rows:
        snippet = (r.get("result_snippet") or "").replace("\n", " ")[:40]
        updated = (r.get("updated_at") or "")[:19]
        typer.echo(
            f"{r['task_id']:<20.20} {r['target_agent']:<22.22} "
            f"{r['target_generation']:>4} {r['state']:<12.12} {updated:<20} {snippet}"
        )


@conv_app.command("show")
def conversations_show(
    task_id: str = typer.Argument(..., help="Task ID."),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSON."),
) -> None:
    """Single conversation detail including its event log."""
    import json as _json

    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        body = api.get_conversation(token=token, task_id=task_id)
    except httpx.HTTPStatusError as exc:
        typer.echo(f"failed: {exc.response.text}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if raw:
        typer.echo(_json.dumps(body, indent=2))
        return

    conv = body["conversation"]
    typer.echo(f"task:      {conv['task_id']}")
    typer.echo(f"target:    {conv['target_agent']} (gen {conv['target_generation']})")
    typer.echo(f"caller:    {conv.get('caller_agent') or '—'}")
    typer.echo(f"state:     {conv['state']}")
    if conv.get("cabal_escalated_from"):
        typer.echo(f"esc from:  {conv['cabal_escalated_from']}")
    typer.echo(f"created:   {conv.get('created_at')}")
    typer.echo(f"updated:   {conv.get('updated_at')}")
    typer.echo("")
    typer.echo("events:")
    for e in body.get("events", []):
        ts = (e.get("created_at") or "")[:19]
        typer.echo(f"  {ts}  {e['event_type']}  {e.get('payload') or {}}")

    result = conv.get("result")
    if result:
        msg = (result.get("status") or {}).get("message") or {}
        parts = msg.get("parts") or []
        text = "\n".join(p.get("text", "") for p in parts if p.get("kind") == "text")
        if text:
            typer.echo("")
            typer.echo("reply:")
            for line in text.splitlines():
                typer.echo(f"  {line}")


@app.command("tail")
def tail() -> None:
    """Live-tail the events stream until Ctrl-C."""
    import json as _json

    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    typer.echo("tailing events (Ctrl-C to stop)…")
    try:
        for event_name, payload in api.tail_events(token=token):
            if event_name == "hello":
                continue
            task_id = payload.get("task_id") or "—"
            inner = payload.get("payload") or {}
            typer.echo(f"{event_name:<24} task={task_id} {_json.dumps(inner)}")
    except KeyboardInterrupt:
        typer.echo("\nbye.")
        return
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon: {exc}", err=True)
        raise typer.Exit(code=1) from exc


primus_app = typer.Typer(help="Primus (persona) lifecycle.")
app.add_typer(primus_app, name="primus")


@primus_app.command("rotate")
def primus_rotate(
    persona: str = typer.Argument(
        ..., help="Persona name (e.g. 'mason') or full agent name ('mason-primus')."
    ),
    hard: bool = typer.Option(
        False, "--hard", help="Phase L v2: cancel in-flight Tasks. Currently a stub."
    ),
) -> None:
    """Rotate a primus generation. Old goes draining, new active gen is born."""
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        body = api.primus_rotate(token=token, persona=persona, hard=hard)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"rotate failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"rotated {body['agent']}: gen {body['previous_generation']} → {body['new_generation']}"
    )
    if body.get("note"):
        typer.echo(f"  note: {body['note']}")


persona_app = typer.Typer(help="Persona lifecycle — graduate and absorb sovereign peers.")
app.add_typer(persona_app, name="persona")


@persona_app.command("graduate")
def persona_graduate(
    name: str = typer.Argument(..., help="Persona name (e.g. 'archivist')."),
    endpoint: str = typer.Option(
        ..., "--endpoint", help="Sovereign service HTTPS URL (e.g. 'https://archivist.rd.ts.net')."
    ),
    audience: str = typer.Option(
        ...,
        "--audience",
        help=(
            "Sovereign service stable id — callers mint this into dispatch token "
            "`aud` (e.g. 'archivist@rd')."
        ),
    ),
) -> None:
    """Graduate a persona to a federated sovereign peer (cabal#45).

    Retires the local member row, registers a federated row pointing at
    the sovereign service, and drops the in-process local handler so all
    dispatch routes to the remote endpoint.
    """
    token = _require_token()
    try:
        body = api.persona_graduate(
            token=token, name=name, endpoint=endpoint, audience=audience
        )
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"graduate failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"graduated {body['agent']} → {body['endpoint']} (aud: {body['audience']})")
    if body.get("previous_generation_retired") is not None:
        typer.echo(f"  retired local gen {body['previous_generation_retired']}, new federated gen {body['generation']}")


@persona_app.command("absorb")
def persona_absorb(
    name: str = typer.Argument(..., help="Persona name to re-absorb (e.g. 'archivist')."),
) -> None:
    """Re-absorb a federated persona back as a local member (cabal#45).

    Retires the active federated row. Run `cbl reload identity` afterwards
    to restore the local handler and member bulletin row.
    """
    token = _require_token()
    try:
        body = api.persona_absorb(token=token, name=name)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"absorb failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"absorbed {body['agent']} (retired federated gen {body['retired_generation']})")
    if body.get("note"):
        typer.echo(f"  → {body['note']}")


secret_app = typer.Typer(help="Persisted, encrypted secrets for credentialed handlers.")
app.add_typer(secret_app, name="secret")


def _require_token() -> str:
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    return token


@secret_app.command("set")
def secret_set(
    persona: str = typer.Argument(..., help="Persona name (e.g. 'archivist')."),
    handler: str = typer.Argument(..., help="Handler name (e.g. 'slack')."),
    key: str = typer.Argument(..., help="KEY in upper-snake (e.g. 'SLACK_BOT_TOKEN')."),
    value: str = typer.Option(
        None,
        "--value",
        help=(
            "Value to store. If omitted you'll be prompted (hidden input; "
            "preferred for tokens since shell history won't capture it)."
        ),
    ),
    purpose: str = typer.Option(
        None, "--purpose", help="Free-form note about what this secret is for."
    ),
) -> None:
    """Persist an encrypted secret keyed by (persona, handler, KEY)."""
    if value is None:
        value = typer.prompt("value", hide_input=True, confirmation_prompt=False)
    if not value:
        typer.echo("value must be non-empty", err=True)
        raise typer.Exit(code=1)
    token = _require_token()
    try:
        api.secret_set(
            token=token,
            persona=persona,
            handler=handler,
            key=key,
            value=value,
            purpose=purpose,
        )
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"set failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"stored: {persona}:{handler}:{key}")


@secret_app.command("unset")
def secret_unset(
    persona: str = typer.Argument(..., help="Persona name."),
    handler: str = typer.Argument(..., help="Handler name."),
    key: str = typer.Argument(..., help="KEY to remove."),
) -> None:
    token = _require_token()
    try:
        body = api.secret_unset(token=token, persona=persona, handler=handler, key=key)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"unset failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    if body.get("deleted"):
        typer.echo(f"deleted: {persona}:{handler}:{key}")
    else:
        typer.echo(f"(no such secret: {persona}:{handler}:{key})")


@secret_app.command("list")
def secret_list(
    persona: str = typer.Option(None, "--persona", help="Filter by persona."),
    handler: str = typer.Option(None, "--handler", help="Filter by handler."),
) -> None:
    """List stored secret NAMES. Values are never printed."""
    token = _require_token()
    try:
        body = api.secret_list(token=token, persona=persona, handler=handler)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"list failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    entries = body.get("entries", [])
    if not entries:
        typer.echo("(no secrets stored)")
        return
    header = f"{'PERSONA':<14} {'HANDLER':<14} KEY"
    typer.echo(header)
    typer.echo("-" * len(header))
    for e in entries:
        typer.echo(f"{e['persona']:<14} {e['handler']:<14} {e['key']}")


token_app = typer.Typer(help="Session token management.")
app.add_typer(token_app, name="token")


@token_app.command("mint")
def token_mint() -> None:
    """Mint a session_token for long-running Tasks. (Phase 2)"""
    typer.echo("cbl token mint: not yet implemented (Phase 2)", err=True)
    raise typer.Exit(code=2)


@token_app.command("mint-service")
def token_mint_service(
    sub: str = typer.Option(
        "service:liminal",
        "--sub",
        help="`sub` claim. Default 'service:liminal' for the canonical use case.",
    ),
    scope: list[str] = typer.Option(
        ["secrets-fetch"],
        "--scope",
        help="`cabal_scope` array; repeat the flag for multiple. Default: secrets-fetch.",
    ),
    aud: str = typer.Option(
        "cabal-core",
        "--aud",
        help="`aud` claim. Default 'cabal-core' — the secrets endpoint accepts that.",
    ),
    ttl_days: int = typer.Option(
        30,
        "--ttl-days",
        help="Token lifetime in days. Service tokens are long-lived (operator-rotated).",
    ),
    persona: str | None = typer.Option(
        None,
        "--persona",
        help=(
            "Optional `cabal_persona` claim. Set to a persona name when minting "
            "a dispatch token for liminal (e.g. `--persona demo --scope dispatch "
            "--aud liminal`). Omit for secrets-fetch service tokens."
        ),
    ),
    allowed_handler: list[str] = typer.Option(
        [],
        "--allowed-handler",
        help=(
            "A liminal handler the token may call (e.g. `web`); repeat for more. "
            "Without it the token carries no handler list, and liminal lets it "
            "call every handler."
        ),
    ),
) -> None:
    """Mint a long-lived service JWT for liminal (or any peer service).

    Prints the JWT to stdout. The operator copies it into the peer's
    env (liminal expects `LIMINAL_CABAL_TOKEN` by default; configurable
    via `liminal serve --cabal-token-env <NAME>`).

    To mint a persona dispatch token for the demo:
        cbl token mint-service --persona demo --scope dispatch --aud liminal --ttl-days 1

    Requires an active daemon — this command hits an admin endpoint
    that signs with the daemon's active signing key.
    """
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        body = api.mint_service_token(
            token=token,
            sub=sub,
            scope=list(scope),
            aud=aud,
            ttl_days=ttl_days,
            persona=persona,
            allowed_handlers=list(allowed_handler) or None,
        )
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"mint failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(body["access_token"])
    typer.echo(
        f"# sub={body['sub']} scope={body['scope']} aud={body['aud']} expires={body['expires_at']}"
        + (f" handlers={body['allowed_handlers']}" if body.get("allowed_handlers") is not None else ""),
        err=True,
    )


reload_app = typer.Typer(help="Hot-reload subsystems.")
app.add_typer(reload_app, name="reload")


@reload_app.command("identity")
def reload_identity() -> None:
    """Re-read identity.toml and rebuild persona handlers in place."""
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        body = api.reload_identity(token=token)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"reload failed: {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        typer.echo(f"could not reach daemon at {api.base_url()}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    registered = body.get("registered", [])
    if registered:
        typer.echo(f"reloaded. registered: {', '.join(registered)}")
    else:
        typer.echo("reloaded. (no LLM-backed personas registered)")
    if body.get("warning"):
        typer.echo(f"  warning: {body['warning']}")


archivist_app = typer.Typer(help="Archivist database maintenance, run on the archivist host.")
app.add_typer(archivist_app, name="archivist")


@archivist_app.command("purge")
def archivist_purge(
    resource_name: str = typer.Argument(..., help="Exact resource_name to remove."),
    db: Path = typer.Option(
        None,
        "--db",
        help="Archivist database file. Default: $CABAL_ARCHIVIST_DB.",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Delete. Without it, only list what would be removed."
    ),
) -> None:
    """Hard-delete every version of an entry, its FTS rows and its relations (#351).

    For text the archivist must not hold (personal data, a leaked secret). To
    retire an entry that is no longer true, supersede it with a tombstone
    through archivist.publish instead.
    """
    from cabal.archivist.purge import purge

    if db is None:
        env = os.environ.get("CABAL_ARCHIVIST_DB", "")
        if not env:
            typer.echo("no database: pass --db or set CABAL_ARCHIVIST_DB", err=True)
            raise typer.Exit(code=2)
        db = Path(env)
    try:
        result = purge(db, resource_name, apply=apply)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    for e in result.entries:
        typer.echo(f"{e.id}  {e.entry_type:<10} {e.status:<10} {e.content_hash}  {e.created_at}")
    verb = "removed" if apply else "would remove"
    typer.echo(
        f"{verb} {len(result.entries)} entries and {result.relations} relations "
        f"for {resource_name!r}"
    )
    if not apply and result.entries:
        typer.echo("re-run with --apply to delete")
