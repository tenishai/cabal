"""Cabal TUI entry point.

The ``cabal`` console script is registered as ``cabal.tui.main:app``.
Running bare ``cabal`` (no subcommand) launches the interactive Textual TUI.
"""
from __future__ import annotations

import typer

app = typer.Typer(
    name="cabal",
    help="Cabal personal agent — interactive TUI and control-plane CLI.",
    add_completion=False,
    pretty_exceptions_show_locals=False,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    persona: str = typer.Option(
        "archivist",
        "--persona",
        help="Default persona to connect to.",
        show_default=True,
    ),
    config: str | None = typer.Option(
        None,
        "--config",
        help=(
            "Path to identity.toml. Default: ./identity.toml when the working "
            "directory holds cabal state (identity.toml, data/cabal.db or "
            "data/.master_key), else $CABAL_HOME/identity.toml (~/.cabal)."
        ),
    ),
    db_url: str | None = typer.Option(
        None,
        "--db-url",
        help="SQLAlchemy database URL. Default: CABAL_DB_URL, else data/cabal.db under the state root.",
    ),
) -> None:
    """Launch the Cabal TUI.

    Running ``cabal`` with no subcommand opens the interactive terminal UI.
    Subcommands (if any) are delegated normally when present.
    """
    # Delegate to a subcommand when one is present.
    if ctx.invoked_subcommand is not None:
        return

    # Guard: give a friendly hint if Textual is missing.
    try:
        import textual  # noqa: F401
    except ImportError:
        typer.echo(
            "Textual is not installed.\n"
            "Install the TUI dependencies with:\n\n"
            "    pip install 'tenish-cabal[tui]'\n",
            err=True,
        )
        raise typer.Exit(code=1) from None

    # Lazy imports so the module is importable even when the broker or
    # Textual isn't available (e.g., during unit tests of the CLI layer).
    from cabal.tui.app import CabalTUI
    from cabal.tui.client.base import BrokerError
    from cabal.tui.client.inprocess import InProcessBrokerClient

    try:
        broker = InProcessBrokerClient(
            default_persona=persona,
            config_path=config,
            db_url=db_url,
        )
    except BrokerError as exc:
        typer.echo(f"cabal failed to start: {exc}", err=True)
        raise typer.Exit(code=1) from None
    tui = CabalTUI(broker=broker, persona=persona)
    tui.run()
    # A boot failure inside the app exits it with a message and a non-zero
    # code (cabal#36); pass the code on so a script can tell.
    if tui.return_code:
        raise typer.Exit(code=tui.return_code)
