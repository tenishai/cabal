"""CabalTUI — the root Textual application."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding

from cabal.tui.panes.primus import PrimusPane


class CabalTUI(App[None]):
    """Cabal terminal user interface.

    Args:
        broker: A :class:`~cabal.tui.client.base.BrokerClient` implementation.
                In production this is an :class:`~cabal.tui.client.inprocess.InProcessBrokerClient`;
                in tests it is any object that satisfies the protocol.
        persona: The active persona name, shown in the transcript and used
                 for routing messages.
    """

    CSS_PATH = "styles.css"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, broker: object, persona: str = "archivist") -> None:
        super().__init__()
        self.broker = broker
        self.persona = persona
        self._broker_entered = False

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield PrimusPane(broker=self.broker, persona=self.persona)

    # ------------------------------------------------------------------
    # Lifecycle: enter / exit the broker alongside the app
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        """Enter the broker context, then open with a greeting.

        Sending a ``Hello`` as the user on launch makes the persona introduce
        itself immediately, instead of presenting an empty ("dead") window.
        """
        # A boot failure (unset CABAL_MASTER_KEY, a key that cannot decrypt
        # the database, ...) exits with its message and status 1 rather than
        # a raw traceback (cabal#36).
        try:
            await self.broker.__aenter__()
        except Exception as exc:  # noqa: BLE001
            self.exit(return_code=1, message=f"cabal failed to start: {exc}")
            return
        self._broker_entered = True
        await self.query_one(PrimusPane).send_user_message("Hello")

    async def on_unmount(self) -> None:
        """Exit the broker context when the app stops, if it was entered."""
        if self._broker_entered:
            await self.broker.__aexit__(None, None, None)
