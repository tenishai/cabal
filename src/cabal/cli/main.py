"""`cbl` CLI entry point.

Phase 1 surface (per cabal-handoff.md §4):
    cbl daemon up        — launch Cabal Core (solo mode by default)
    cbl login            — trade a bootstrap code for an operator JWT
    cbl dispatch         — send a one-shot A2A Task and print the result
    cbl version          — print version
    cbl token mint       — (Phase 2) mint a session_token
    cbl reload identity  — (Phase 2) hot-reload identity.toml
"""

from __future__ import annotations

import json
from pathlib import Path

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


@daemon_app.command("up")
def daemon_up(
    agent: str = typer.Option(
        "all",
        "--agent",
        help="'all' for solo mode (every persona local), or a single agent name "
        "(e.g. 'rook-primus') for scale mode.",
    ),
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(7400, help="Bind port."),
    config: str = typer.Option("identity.toml", help="Path to identity.toml."),
) -> None:
    """Launch Cabal Core."""
    import uvicorn

    from cabal.core.app import create_app

    fastapi_app = create_app(agent=agent, config_path=config)
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@app.command("login")
def login(
    code: str = typer.Option(
        None,
        "--code",
        prompt="Bootstrap code",
        help="6-character code printed in the daemon log on startup.",
    ),
) -> None:
    """Trade a bootstrap code for an operator JWT (saved to ~/.cabal/token)."""
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

    api.TokenStore().save(body["access_token"])
    persona = body.get("persona")
    typer.echo(
        f"Logged in as {'operator' if persona is None else 'persona ' + persona}. "
        f"Token saved to {api.DEFAULT_TOKEN_PATH}."
    )


@app.command("dispatch")
def dispatch(
    target: str = typer.Argument(..., help="Target agent name (e.g. 'echo')."),
    message: str = typer.Argument(..., help="Initial user message."),
    raw: bool = typer.Option(False, "--raw", help="Print full Task JSON."),
) -> None:
    """Send a one-shot A2A Task and print the response."""
    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)
    try:
        response = api.dispatch(token=token, target=target, message_text=message)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        typer.echo(f"dispatch failed: {detail}", err=True)
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
            agent_text = _stream_chat_turn(target, token, user_text, buffered=buffer_stream)
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
    target: str, token: str, user_text: str, *, buffered: bool = False
) -> str | None:
    """Run one streaming turn. When `buffered=False` (default), prints
    deltas as they arrive. When `buffered=True`, accumulates silently —
    the caller renders the assembled text (used for edit-format
    parsing, which can't run mid-stream). Returns the full assembled
    agent text or None on error."""
    import sys

    if not buffered:
        typer.echo(f"{target}: ", nl=False)
        sys.stdout.flush()
    full_text = ""
    try:
        for event_name, payload in api.stream_dispatch(
            token=token, target=target, message_text=user_text
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
    header = f"{'NAME':<22} {'GEN':>4} {'ROLE':<10} {'STATUS':<10} {'TIER':<8} HEARTBEAT"
    typer.echo(header)
    typer.echo("-" * len(header))
    for a in agents:
        hb = a.get("last_heartbeat") or "—"
        typer.echo(
            f"{a['name']:<22} {a['generation']:>4} {a['role']:<10} "
            f"{a['status']:<10} {a['safety_tier']:<8} {hb}"
        )


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
        ..., help="Persona name (e.g. 'rook') or full agent name ('rook-primus')."
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
        "service:limenal",
        "--sub",
        help="`sub` claim. Default 'service:limenal' for the canonical use case.",
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
            "a dispatch token for limenal (e.g. `--persona demo --scope dispatch "
            "--aud limenal`). Omit for secrets-fetch service tokens."
        ),
    ),
) -> None:
    """Mint a long-lived service JWT for limenal (or any peer service).

    Prints the JWT to stdout. The operator copies it into the peer's
    env (limenal expects `LIMENAL_CABAL_TOKEN` by default; configurable
    via `limenal serve --cabal-token-env <NAME>`).

    To mint a persona dispatch token for the demo:
        cbl token mint-service --persona demo --scope dispatch --aud limenal --ttl-days 1

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
        f"# sub={body['sub']} scope={body['scope']} aud={body['aud']} expires={body['expires_at']}",
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
