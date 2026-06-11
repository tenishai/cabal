"""`cbl` CLI entry point.

Phase 1 surface (per cabal-handoff.md §4):
    cbl daemon up        — launch Cabal Core (solo mode by default)
    cbl login            — operator OAuth flow against Cabal Core
    cbl dispatch         — send a one-shot A2A Task and stream the result
    cbl token mint       — mint a session_token for long-running Tasks
    cbl reload identity  — re-read identity.toml without restarting

Later-phase commands (primus rotate, persona add, etc.) are stubs that
exit nonzero with a "not yet implemented" message so we surface the
surface contract in --help before the implementation lands.
"""

from __future__ import annotations

import typer

from cabal import __version__

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
def login() -> None:
    """Operator OAuth flow against Cabal Core. (Phase 1: stub)"""
    typer.echo("cbl login: not yet implemented (Phase 1)", err=True)
    raise typer.Exit(code=2)


@app.command("dispatch")
def dispatch(
    target: str = typer.Argument(..., help="Target agent name."),
    message: str = typer.Argument(..., help="Initial user message."),
) -> None:
    """Send a one-shot A2A Task and stream the result. (Phase 1: stub)"""
    typer.echo(f"cbl dispatch -> {target}: not yet implemented (Phase 1)", err=True)
    raise typer.Exit(code=2)


token_app = typer.Typer(help="Session token management.")
app.add_typer(token_app, name="token")


@token_app.command("mint")
def token_mint() -> None:
    """Mint a session_token for long-running Tasks. (Phase 1: stub)"""
    typer.echo("cbl token mint: not yet implemented (Phase 1)", err=True)
    raise typer.Exit(code=2)


reload_app = typer.Typer(help="Hot-reload subsystems.")
app.add_typer(reload_app, name="reload")


@reload_app.command("identity")
def reload_identity() -> None:
    """Re-read identity.toml without restarting. (Phase 1: stub)"""
    typer.echo("cbl reload identity: not yet implemented (Phase 1)", err=True)
    raise typer.Exit(code=2)
