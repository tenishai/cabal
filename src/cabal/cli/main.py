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
    config: str = typer.Option(
        "identity.toml", help="Path to identity.toml."
    ),
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
) -> None:
    """Multi-turn chat with a persona. Empty line or :exit to quit."""
    import sys
    import uuid

    import click

    token = api.TokenStore().load()
    if token is None:
        typer.echo("no token; run `cbl login` first.", err=True)
        raise typer.Exit(code=1)

    context_id = f"chat-{uuid.uuid4().hex[:12]}"
    history: list[dict] = []
    typer.echo(f"chat with {target} (context: {context_id}). type :exit to quit.")
    typer.echo("─" * 60)

    while True:
        try:
            user_text = typer.prompt(">", prompt_suffix=" ", show_default=False)
        except (EOFError, click.exceptions.Abort):
            typer.echo("\nbye.")
            return
        user_text = user_text.strip()
        if not user_text or user_text in {":exit", ":quit", ":q"}:
            typer.echo("bye.")
            return

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

        history.append(
            {"role": "user", "parts": [{"kind": "text", "text": user_text}]}
        )
        message = response.get("status", {}).get("message") or {}
        agent_text = "".join(
            part.get("text", "")
            for part in message.get("parts", [])
            if part.get("kind") == "text"
        )
        if message.get("parts"):
            history.append(
                {"role": message.get("role", "agent"), "parts": message["parts"]}
            )

        state = response.get("status", {}).get("state", "?")
        prefix = target if state == "completed" else f"{target} [{state}]"
        typer.echo(f"{prefix}: {agent_text}")
        typer.echo("")
        sys.stdout.flush()


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
        f"rotated {body['agent']}: gen {body['previous_generation']} → "
        f"{body['new_generation']}"
    )
    if body.get("note"):
        typer.echo(f"  note: {body['note']}")


token_app = typer.Typer(help="Session token management.")
app.add_typer(token_app, name="token")


@token_app.command("mint")
def token_mint() -> None:
    """Mint a session_token for long-running Tasks. (Phase 2)"""
    typer.echo("cbl token mint: not yet implemented (Phase 2)", err=True)
    raise typer.Exit(code=2)


reload_app = typer.Typer(help="Hot-reload subsystems.")
app.add_typer(reload_app, name="reload")


@reload_app.command("identity")
def reload_identity() -> None:
    """Re-read identity.toml without restarting. (Phase 2)"""
    typer.echo("cbl reload identity: not yet implemented (Phase 2)", err=True)
    raise typer.Exit(code=2)
