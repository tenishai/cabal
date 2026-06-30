"""Streaming text rendering helpers for the Cabal TUI transcript.

The primary helper is :class:`StreamBuffer`, which accumulates streaming text
deltas and keeps a ``Static`` widget updated in real time so partial agent
responses appear as they arrive.
"""
from __future__ import annotations

from textual.widgets import Static


class StreamBuffer:
    """Accumulates streaming text deltas and refreshes a :class:`~textual.widgets.Static` live.

    Usage::

        buf = StreamBuffer(response_widget, prefix="archivist: ")
        async for delta in broker.send_message(persona, text):
            buf.append(delta)

    Each call to :meth:`append` concatenates *delta* onto the internal
    buffer and immediately calls :meth:`~textual.widgets.Static.update` on
    the bound widget, so the UI reflects the partial text without waiting
    for the full response.
    """

    def __init__(self, widget: Static, prefix: str = "") -> None:
        """
        Args:
            widget: The Static widget to update on each delta.
            prefix: Text prepended before the accumulated content on every
                    update (e.g. ``"archivist: "`` for the persona label).
        """
        self._widget = widget
        self._prefix = prefix
        self._text = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, delta: str) -> None:
        """Append *delta* to the buffer and refresh the widget."""
        self._text += delta
        self._widget.update(f"{self._prefix}{self._text}")

    @property
    def text(self) -> str:
        """The full accumulated text received so far."""
        return self._text
