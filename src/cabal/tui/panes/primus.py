"""PrimusPane — scrollable transcript + bottom input widget."""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Input, Static

from cabal.tui.rendering.markdown import StreamBuffer

# BrokerError is defined in cabal.tui.client.base (other agent's deliverable).
# Provide a graceful fallback so tests and parallel builds work without it.
try:
    from cabal.tui.client.base import BrokerError
except ImportError:  # pragma: no cover — only absent during parallel dev
    class BrokerError(RuntimeError):  # type: ignore[misc]
        """Fallback until cabal.tui.client.base is available."""


class PrimusPane(Widget):
    """Chat pane: scrollable message transcript above a fixed input bar.

    Args:
        broker: A :class:`~cabal.tui.client.base.BrokerClient` implementation.
        persona: The default persona name to route messages to.
    """

    def __init__(self, broker: object, persona: str) -> None:
        super().__init__()
        self.broker = broker
        self.persona = persona

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="transcript"):
            pass
        yield Input(placeholder=f"Message {self.persona}…", id="msg-input")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle the user pressing Enter in the input bar."""
        text = event.value.strip()
        if not text:
            return

        # Clear the input immediately so the user can type the next message.
        event.input.clear()

        # Append the user turn to the transcript.
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(
            Static(f"[bold cyan]you[/bold cyan]: {text}", markup=True)
        )
        transcript.scroll_end(animate=False)

        # Kick off the streaming worker.
        self._stream_response(text)

    # ------------------------------------------------------------------
    # Streaming worker
    # ------------------------------------------------------------------

    @work(exclusive=False)
    async def _stream_response(self, text: str) -> None:
        """Async worker: streams deltas from the broker into the transcript."""
        transcript = self.query_one("#transcript", VerticalScroll)

        # Create the agent-turn widget up front so it appears immediately.
        response_widget = Static(
            f"[bold green]{self.persona}[/bold green]: ",
            markup=True,
        )
        await transcript.mount(response_widget)
        transcript.scroll_end(animate=False)

        buf = StreamBuffer(
            widget=response_widget,
            prefix=f"[bold green]{self.persona}[/bold green]: ",
        )
        try:
            async for delta in self.broker.send_message(self.persona, text):
                buf.append(delta)
                transcript.scroll_end(animate=False)
        except BrokerError as exc:
            response_widget.update(f"[red bold]error[/red bold]: {exc}")
