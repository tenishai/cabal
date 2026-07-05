"""cbl chat interactive loop tests.

Uses Typer's CliRunner with stdin input + a stubbed api.dispatch so we
don't need a running daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import cabal.cli.client as api
from cabal.cli.main import app


@pytest.fixture
def fake_token(monkeypatch, tmp_path: Path) -> Path:
    token_path = tmp_path / "token"
    token_path.write_text("fake.jwt.value")
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", token_path)
    return token_path


def _stub_dispatch(replies: list[str]) -> tuple[Any, list[dict]]:
    """Returns a stub dispatch fn and the captured-calls list."""
    iterator = iter(replies)
    calls: list[dict] = []

    def fake(*, token, target, message_text, history=None, context_id=None, metadata=None):
        text = next(iterator, "(out of replies)")
        calls.append(
            {
                "target": target,
                "user": message_text,
                "history_len": len(history or []),
                "context_id": context_id,
            }
        )
        return {
            "id": "t-fake",
            "status": {
                "state": "completed",
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": text}],
                },
            },
        }

    return fake, calls


def test_chat_single_turn_exits_on_quit(fake_token, monkeypatch) -> None:
    fake, calls = _stub_dispatch(["I remember the first thing."])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chat", "archivist", "--no-stream"],
        input="what do you remember?\n:exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "archivist: I remember the first thing." in result.output
    assert len(calls) == 1
    assert calls[0]["history_len"] == 0  # first turn has no history


def test_chat_accumulates_history(fake_token, monkeypatch) -> None:
    fake, calls = _stub_dispatch(["first reply", "second reply"])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chat", "archivist", "--no-stream"],
        input="first\nsecond\n:exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "first reply" in result.output
    assert "second reply" in result.output
    assert len(calls) == 2
    # Second turn has the first user msg + agent reply in history
    assert calls[1]["history_len"] == 2


def test_chat_reuses_context_id_across_turns(fake_token, monkeypatch) -> None:
    fake, calls = _stub_dispatch(["a", "b"])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    runner.invoke(app, ["chat", "x", "--no-stream"], input="hi\nyo\n:exit\n")
    assert len(calls) == 2
    assert calls[0]["context_id"] == calls[1]["context_id"]
    assert calls[0]["context_id"].startswith("chat-")


def test_chat_without_token_errors(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api, "DEFAULT_TOKEN_PATH", tmp_path / "absent")
    runner = CliRunner()
    result = runner.invoke(app, ["chat", "echo"], input="hi\n")
    assert result.exit_code == 1
    assert "no token" in result.output.lower()


def test_chat_empty_input_exits(fake_token, monkeypatch) -> None:
    fake, calls = _stub_dispatch([])
    monkeypatch.setattr(api, "dispatch", fake)
    runner = CliRunner()
    result = runner.invoke(app, ["chat", "x", "--no-stream"], input="\n")
    assert result.exit_code == 0
    assert len(calls) == 0  # didn't even dispatch


# ---- streaming variant (Phase T) -------------------------------------------


def _stub_stream(chunks: list[str]):
    """Mimic api.stream_dispatch — yield (event, payload) tuples."""

    def fake(*, token, target, message_text, context_id=None):
        for c in chunks:
            yield ("chunk", {"delta": c})
        yield (
            "status-update",
            {"result": {"status": {"state": "completed"}}, "final": True},
        )

    return fake


def test_chat_streaming_prints_deltas(fake_token, monkeypatch) -> None:
    monkeypatch.setattr(api, "stream_dispatch", _stub_stream(["Hello ", "operator!"]))
    runner = CliRunner()
    result = runner.invoke(app, ["chat", "archivist"], input="hi\n:exit\n")
    assert result.exit_code == 0, result.output
    assert "Hello operator!" in result.output
    assert "streaming" in result.output  # mode note


def test_chat_streaming_error_event_keeps_prompt_open(fake_token, monkeypatch) -> None:
    """An error event should print the message but not crash the chat loop."""

    def err_stream(*, token, target, message_text, context_id=None):
        yield ("error", {"error": {"code": -32003, "message": "agent crashed"}})

    monkeypatch.setattr(api, "stream_dispatch", err_stream)
    runner = CliRunner()
    result = runner.invoke(app, ["chat", "x"], input="hi\n:exit\n")
    assert result.exit_code == 0, result.output
    assert "agent crashed" in result.output


def test_chat_no_stream_flag_falls_back_to_dispatch(fake_token, monkeypatch) -> None:
    """--no-stream uses the non-streaming dispatch path."""
    fake_disp, calls = _stub_dispatch(["one-shot reply"])
    monkeypatch.setattr(api, "dispatch", fake_disp)

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("stream_dispatch must not be called with --no-stream")

    monkeypatch.setattr(api, "stream_dispatch", should_not_be_called)

    runner = CliRunner()
    result = runner.invoke(app, ["chat", "echo", "--no-stream"], input="hi\n:exit\n")
    assert result.exit_code == 0, result.output
    assert "one-shot reply" in result.output
    assert "single-shot" in result.output
    assert len(calls) == 1


# ---- edit_format (cabal#4) --------------------------------------------------


_SEARCH_REPLACE_REPLY = (
    "Here's the change:\n\n<<<<<<< SEARCH foo.py\nold\n=======\nnew\n>>>>>>> REPLACE\n"
)


def test_chat_edit_format_no_apply_dry_run_skips_disk(fake_token, monkeypatch, tmp_path) -> None:
    """--no-apply parses + reports blocks but never touches disk."""
    target = tmp_path / "foo.py"
    target.write_text("old\n")

    fake, _calls = _stub_dispatch([_SEARCH_REPLACE_REPLY])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "chat",
            "architect",
            "--no-stream",
            "--edit-format",
            "search-replace",
            "--no-apply",
            "--project-root",
            str(tmp_path),
        ],
        input="please change foo\n:exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "parsed 1 edit block" in result.output
    assert "dry-run, not applied" in result.output
    # Disk unchanged
    assert target.read_text() == "old\n"


def test_chat_edit_format_applies_block_on_confirm(fake_token, monkeypatch, tmp_path) -> None:
    """A 'y' confirm applies the block to disk."""
    target = tmp_path / "foo.py"
    target.write_text("old\n")

    fake, _calls = _stub_dispatch([_SEARCH_REPLACE_REPLY])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "chat",
            "architect",
            "--no-stream",
            "--edit-format",
            "search-replace",
            "--project-root",
            str(tmp_path),
        ],
        # 'y' confirms the apply prompt, then :exit
        input="please change foo\ny\n:exit\n",
    )
    assert result.exit_code == 0, result.output
    assert target.read_text() == "new\n"


def test_chat_edit_format_skip_block_on_decline(fake_token, monkeypatch, tmp_path) -> None:
    """A 'n' decline leaves disk unchanged but the chat continues."""
    target = tmp_path / "foo.py"
    target.write_text("old\n")

    fake, _calls = _stub_dispatch([_SEARCH_REPLACE_REPLY])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "chat",
            "architect",
            "--no-stream",
            "--edit-format",
            "search-replace",
            "--project-root",
            str(tmp_path),
        ],
        input="please change foo\nn\n:exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "(skipped)" in result.output
    assert target.read_text() == "old\n"


def test_chat_edit_format_failed_match_renders_context(fake_token, monkeypatch, tmp_path) -> None:
    """When SEARCH doesn't match, show the actual file content inline."""
    target = tmp_path / "foo.py"
    target.write_text("DIFFERENT content\n")

    fake, _calls = _stub_dispatch([_SEARCH_REPLACE_REPLY])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "chat",
            "architect",
            "--no-stream",
            "--edit-format",
            "search-replace",
            "--project-root",
            str(tmp_path),
        ],
        input="change\ny\n:exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "not found" in result.output
    assert "DIFFERENT content" in result.output
    assert target.read_text() == "DIFFERENT content\n"


def test_chat_edit_format_no_blocks_falls_back_to_raw_text(
    fake_token, monkeypatch, tmp_path
) -> None:
    """If the assistant just asks a question (no blocks), render its
    text as-is. Useful for clarifying-question turns."""
    fake, _calls = _stub_dispatch(["which file did you mean?"])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "chat",
            "architect",
            "--no-stream",
            "--edit-format",
            "search-replace",
            "--project-root",
            str(tmp_path),
        ],
        input="rename frob\n:exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "which file did you mean?" in result.output
    assert "parsed" not in result.output  # no block summary


def test_chat_edit_format_invalid_value_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chat", "x", "--edit-format", "udiff"],
        input="hi\n",
    )
    assert result.exit_code == 2
    assert "unknown --edit-format" in result.output
