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
        yield Input(
            placeholder=f"Message {self.persona}…  (/exit to quit)",
            id="msg-input",
        )

    def on_mount(self) -> None:
        """Focus the message input on launch so the user can type immediately.

        Without this, focus lands on the scrollable transcript and the user
        has to press Tab to reach the input bar.
        """
        self.query_one("#msg-input", Input).focus()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle the user pressing Enter in the input bar."""
        text = event.value.strip()
        if not text:
            return

        # Slash commands are handled locally and never sent to the broker.
        if text.lower() in ("/exit", "/quit", "/q"):
            event.input.clear()
            self.app.exit()
            return

        # Clear the input immediately so the user can type the next message.
        event.input.clear()
        await self.send_user_message(text)

    async def send_user_message(self, text: str) -> None:
        """Append a user turn to the transcript and stream the persona's reply.

        Shared by the Enter handler and the on-launch greeting so a typed
        message and the opening ``Hello`` render through the same path.
        """
        # Append the user turn to the transcript.
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(
            Static(f"[bold cyan]you[/bold cyan]: {text}", markup=True)
        )
        transcript.scroll_end(animate=False)

        # Serialize turns: disable the input while this turn streams so a second
        # send can't interleave and scramble in-session history (Prudence #101).
        # Re-enabled in _stream_response's finally.
        self.query_one("#msg-input", Input).disabled = True

        # Kick off the streaming worker.
        self._stream_response(text)

    # ------------------------------------------------------------------
    # Streaming worker
    # ------------------------------------------------------------------

    @work(exclusive=False)
    async def _stream_response(self, text: str) -> None:
        """Async worker: streams deltas from the broker into the transcript."""
        transcript = self.query_one("#transcript", VerticalScroll)
        prefix = f"[bold green]{self.persona}[/bold green]: "

        # Working indicator: a single left-justified dot that blinks under the
        # prompt while we wait for the model (for a primus, also while it plans
        # and delegates to subordinates). Left-aligned like the transcript
        # lines, replaced by the response the moment the first delta arrives.
        dot = Static("[bold green]●[/]", markup=True, classes="working-dot")
        dot.styles.height = 1
        await transcript.mount(dot)
        transcript.scroll_end(animate=False)

        dot_lit = True

        def _blink() -> None:
            nonlocal dot_lit
            dot_lit = not dot_lit
            dot.update("[bold green]●[/]" if dot_lit else " ")

        blink_timer = self.set_interval(0.5, _blink)

        response_widget = Static(prefix, markup=True)
        buf = StreamBuffer(widget=response_widget, prefix=prefix)

        async def _swap_in_response() -> None:
            """Stop the blink, remove the dot, mount the response line (once)."""
            blink_timer.stop()
            await dot.remove()
            await transcript.mount(response_widget)

        pending = True  # dot still showing; response not yet mounted
        try:
            async for delta in self.broker.send_message(self.persona, text):
                if pending:
                    await _swap_in_response()
                    pending = False
                buf.append(delta)
                transcript.scroll_end(animate=False)
        except BrokerError as exc:
            if pending:
                await _swap_in_response()
                pending = False
            response_widget.update(f"[red bold]error[/red bold]: {exc}")
        finally:
            # Empty response (no deltas, no error): still clear the indicator.
            if pending:
                await _swap_in_response()
            # Re-enable + refocus the input now that this turn is done.
            inp = self.query_one("#msg-input", Input)
            inp.disabled = False
            inp.focus()
