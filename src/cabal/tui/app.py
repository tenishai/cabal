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
        await self.broker.__aenter__()
        await self.query_one(PrimusPane).send_user_message("Hello")

    async def on_unmount(self) -> None:
        """Exit the broker context when the app stops."""
        await self.broker.__aexit__(None, None, None)
