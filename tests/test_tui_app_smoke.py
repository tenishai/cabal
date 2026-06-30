"""Headless Textual smoke test for CabalTUI.

Uses a ``FakeBroker`` that satisfies the ``BrokerClient`` protocol without
importing any real cabal backend code. Exercises the full input → worker →
transcript rendering path in headless mode via ``App.run_test()``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

pytest.importorskip("textual")  # optional [tui] extra — auto-skip when absent (e.g. CI)

from textual.containers import VerticalScroll  # noqa: E402
from textual.widgets import Static  # noqa: E402

from cabal.tui.app import CabalTUI  # noqa: E402

# ---------------------------------------------------------------------------
# Fake broker — satisfies the BrokerClient protocol without any real backend
# ---------------------------------------------------------------------------


class FakeBroker:
    """Minimal BrokerClient stub that yields three fixed deltas per message."""

    default_persona: str = "archivist"

    async def __aenter__(self) -> FakeBroker:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def send_message(self, persona: str, text: str) -> AsyncIterator[str]:  # type: ignore[override]
        """Yield three deltas so the streaming path is exercised."""
        for delta in ("Hello", " ", "world"):
            yield delta


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_smoke_app_mounts() -> None:
    """CabalTUI starts, mounts PrimusPane, and shuts down cleanly."""
    async with CabalTUI(broker=FakeBroker(), persona="archivist").run_test() as pilot:
        # The transcript VerticalScroll and input should both be present.
        assert pilot.app.query_one("#transcript", VerticalScroll) is not None
        assert pilot.app.query_one("#msg-input") is not None


async def test_smoke_submit_and_stream() -> None:
    """Typing a message and pressing Enter produces streamed output in the transcript."""
    async with CabalTUI(broker=FakeBroker(), persona="archivist").run_test() as pilot:
        # Focus the input and type a message.
        await pilot.click("#msg-input")
        await pilot.press("h", "i")
        await pilot.press("enter")

        # Give the async streaming worker time to finish.
        await pilot.pause()
        await pilot.pause()

        # Collect all Static widget contents from the transcript.
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        statics = list(transcript.query(Static))
        assert statics, "Expected at least one Static widget in the transcript"

        all_text = " ".join(str(w.content) for w in statics)

        # User message should be present.
        assert "hi" in all_text.lower(), (
            f"User message 'hi' not found in transcript. Got: {all_text!r}"
        )

        # Streamed agent deltas should be present (either mid-stream or final).
        assert "Hello" in all_text, (
            f"Streamed delta 'Hello' not in transcript. Got: {all_text!r}"
        )
        assert "world" in all_text, (
            f"Streamed delta 'world' not in transcript. Got: {all_text!r}"
        )


async def test_smoke_input_cleared_after_submit() -> None:
    """The input bar is empty after the user submits a message."""
    from textual.widgets import Input

    async with CabalTUI(broker=FakeBroker(), persona="archivist").run_test() as pilot:
        await pilot.click("#msg-input")
        await pilot.press("t", "e", "s", "t")
        await pilot.press("enter")
        await pilot.pause()

        inp = pilot.app.query_one("#msg-input", Input)
        assert inp.value == "", f"Input not cleared after submit; value={inp.value!r}"
