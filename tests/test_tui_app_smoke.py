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


async def test_smoke_input_focused_on_launch() -> None:
    """The message input is focused on mount — no Tab needed to start typing."""
    from textual.widgets import Input

    async with CabalTUI(broker=FakeBroker(), persona="archivist").run_test() as pilot:
        await pilot.pause()
        inp = pilot.app.query_one("#msg-input", Input)
        assert pilot.app.focused is inp, (
            f"Expected #msg-input focused on launch; focus is on {pilot.app.focused!r}"
        )
        # Typing without any Tab/click should land in the input.
        await pilot.press("h", "i")
        assert inp.value == "hi", f"Typed text did not reach input; value={inp.value!r}"


class RecordingBroker(FakeBroker):
    """FakeBroker that records every message text it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, persona: str, text: str) -> AsyncIterator[str]:  # type: ignore[override]
        self.sent.append(text)
        for delta in ("Hello", " ", "world"):
            yield delta


async def test_smoke_auto_greeting_on_launch() -> None:
    """On launch the app sends a 'Hello' as the user so the persona replies."""
    broker = RecordingBroker()
    async with CabalTUI(broker=broker, persona="archivist").run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        # The greeting was sent to the broker as the user's first message.
        assert broker.sent == ["Hello"], f"Expected one 'Hello' greeting; got {broker.sent!r}"

        # Both a user turn and a streamed reply are on screen — not a dead window.
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        all_text = " ".join(str(w.content) for w in transcript.query(Static))
        assert "you" in all_text.lower(), f"No user turn rendered; got {all_text!r}"
        assert "Hello" in all_text and "world" in all_text, (
            f"Persona reply not rendered; got {all_text!r}"
        )


async def test_smoke_working_indicator_while_waiting() -> None:
    """A blinking dot shows on the pending agent line until the first delta."""
    import asyncio

    class GatedBroker(FakeBroker):
        """Holds the first delta until released, so we can observe the wait."""

        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def send_message(self, persona: str, text: str) -> AsyncIterator[str]:  # type: ignore[override]
            await self.release.wait()
            yield "done"

    broker = GatedBroker()
    async with CabalTUI(broker=broker, persona="archivist").run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        # The on-launch greeting is parked on the gate -> the dot is visible.
        assert len(pilot.app.query(".working-dot")) >= 1, (
            "Expected a blinking working-dot on the pending agent line while waiting"
        )

        broker.release.set()
        await pilot.pause()
        await pilot.pause()
        # Once the delta arrives and the stream completes, the dot is gone.
        assert len(pilot.app.query(".working-dot")) == 0, (
            "Working dot was not removed after the response arrived"
        )


async def test_smoke_slash_exit_quits_app() -> None:
    """Submitting '/exit' quits the app and is never forwarded to the broker."""
    broker = RecordingBroker()
    async with CabalTUI(broker=broker, persona="archivist").run_test() as pilot:
        await pilot.pause()  # let the on-launch greeting fire first
        await pilot.press("slash", "e", "x", "i", "t")
        await pilot.press("enter")
        await pilot.pause()

        assert "/exit" not in broker.sent, f"'/exit' was forwarded to the broker: {broker.sent!r}"
        # App.exit() flips the shutdown flag; the pilot context exits cleanly.
        assert pilot.app._exit is True, "App did not exit after '/exit'"


class BootFailingBroker(RecordingBroker):
    """A broker whose boot raises, as an unset CABAL_MASTER_KEY does."""

    def __init__(self) -> None:
        super().__init__()
        self.exited = False

    async def __aenter__(self) -> BootFailingBroker:
        raise RuntimeError("CABAL_MASTER_KEY is unset.")

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True


async def test_a_boot_failure_exits_with_its_message_not_a_traceback(capsys) -> None:
    """cabal#36: a failing broker boot exits the app with status 1 and the
    error text, sends no greeting, and does not exit a broker it never
    entered."""
    broker = BootFailingBroker()
    app = CabalTUI(broker=broker, persona="archivist")
    async with app.run_test() as pilot:
        await pilot.pause()
    assert app.return_code == 1
    assert broker.sent == []
    assert broker.exited is False
    err = capsys.readouterr().err
    assert "cabal failed to start: CABAL_MASTER_KEY is unset." in err, err
    assert "Traceback" not in err, err


def test_the_launcher_reports_a_missing_token_and_exits_1(monkeypatch) -> None:
    """cabal#36: the no-token BrokerError is raised before the app exists, so
    the launcher, not on_mount, has to turn it into a message."""
    from typer.testing import CliRunner

    import cabal.tui.client.inprocess as inprocess
    from cabal.tui.client.base import BrokerError
    from cabal.tui.main import app as tui_cli

    def no_token(**_: object) -> None:
        raise BrokerError("no operator token found; run `cbl login` first to authenticate")

    monkeypatch.setattr(inprocess, "InProcessBrokerClient", no_token)
    result = CliRunner().invoke(tui_cli, [])
    assert result.exit_code == 1
    assert "cabal failed to start: no operator token found" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_the_launcher_passes_on_the_apps_exit_code(monkeypatch) -> None:
    """A boot failure inside the app sets return_code 1; the process exits 1."""
    from typer.testing import CliRunner

    import cabal.tui.app as tui_app
    import cabal.tui.client.inprocess as inprocess
    from cabal.tui.main import app as tui_cli

    class FailedApp:
        return_code = 1

        def __init__(self, **_: object) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(inprocess, "InProcessBrokerClient", lambda **_: object())
    monkeypatch.setattr(tui_app, "CabalTUI", FailedApp)
    assert CliRunner().invoke(tui_cli, []).exit_code == 1
