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
    result = runner.invoke(app, ["chat", "archivist"], input="what do you remember?\n:exit\n")
    assert result.exit_code == 0, result.output
    assert "archivist: I remember the first thing." in result.output
    assert len(calls) == 1
    assert calls[0]["history_len"] == 0  # first turn has no history


def test_chat_accumulates_history(fake_token, monkeypatch) -> None:
    fake, calls = _stub_dispatch(["first reply", "second reply"])
    monkeypatch.setattr(api, "dispatch", fake)

    runner = CliRunner()
    result = runner.invoke(
        app, ["chat", "archivist"], input="first\nsecond\n:exit\n"
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
    runner.invoke(app, ["chat", "x"], input="hi\nyo\n:exit\n")
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
    result = runner.invoke(app, ["chat", "x"], input="\n")
    assert result.exit_code == 0
    assert len(calls) == 0  # didn't even dispatch
