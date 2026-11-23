"""`cbl status` and `cbl session` rendering (cabal#142)."""

from __future__ import annotations

from datetime import UTC

from typer.testing import CliRunner

from cabal.cli.main import _format_work, app

runner = CliRunner()


def test_format_work_orders_repo_branch_task() -> None:
    assert (
        _format_work({"task": "hardening bill", "repo": "liminal", "branch": "fix/73"})
        == "liminal · fix/73 · hardening bill"
    )


def test_format_work_keeps_unknown_keys_rather_than_dropping_them() -> None:
    """The payload is deliberately free-form so the shape can settle in use.
    Silently dropping an unrecognised key would make an experiment look empty."""
    out = _format_work({"repo": "cabal", "pr": 142})
    assert "cabal" in out
    assert "pr=142" in out


def test_format_work_on_an_empty_payload_is_a_dash() -> None:
    assert _format_work({}) == "—"


def test_status_renders_a_work_line_under_a_session(monkeypatch) -> None:
    monkeypatch.setattr("cabal.cli.main.api.TokenStore.load", lambda self: "tok")
    monkeypatch.setattr(
        "cabal.cli.main.api.list_agents",
        lambda **_: {
            "agents": [
                {
                    "name": "echo", "generation": 1, "kind": "member", "role": "builtin",
                    "status": "active", "safety_tier": "strict",
                    "last_heartbeat": None, "work": None,
                },
                {
                    "name": "agent-b", "generation": 1, "kind": "session", "role": "session",
                    "status": "active", "safety_tier": "strict",
                    "last_heartbeat": "2026-09-10T12:00:00Z",
                    "work": {"repo": "liminal", "branch": "fix/73", "task": "hardening"},
                },
            ]
        },
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "KIND" in result.output
    assert "session" in result.output
    # the work line is the answer to "what are they doing"
    assert "liminal · fix/73 · hardening" in result.output
    # a persona row with no work gets no stray line
    assert result.output.count("↳") == 1


def test_status_still_works_against_a_daemon_that_omits_kind(monkeypatch) -> None:
    """An older daemon predating this change returns rows with no `kind` or
    `work`. The CLI must render them, not KeyError."""
    monkeypatch.setattr("cabal.cli.main.api.TokenStore.load", lambda self: "tok")
    monkeypatch.setattr(
        "cabal.cli.main.api.list_agents",
        lambda **_: {
            "agents": [
                {
                    "name": "echo", "generation": 1, "role": "builtin",
                    "status": "active", "safety_tier": "strict", "last_heartbeat": None,
                }
            ]
        },
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "member" in result.output


def test_a_session_that_stopped_reporting_reads_as_stale() -> None:
    """Clean de-registration cannot be relied on — a crashed session never runs
    its Stop hook. Staleness is what stops a dead session looking alive."""
    from datetime import datetime, timedelta

    from cabal.cli.main import SESSION_STALE_AFTER_S, _heartbeat_cell

    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    dead = (now - timedelta(seconds=SESSION_STALE_AFTER_S + 60)).isoformat()
    warm = (now - timedelta(seconds=30)).isoformat()

    assert "STALE" in _heartbeat_cell(dead, kind="session", now=now)
    assert "STALE" not in _heartbeat_cell(warm, kind="session", now=now)


def test_a_persona_heartbeat_is_never_marked_stale() -> None:
    """Personas are heartbeated by the scheduler on a different cadence; the
    session threshold would mislabel them."""
    from datetime import datetime, timedelta

    from cabal.cli.main import _heartbeat_cell

    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=1)).isoformat()
    assert "STALE" not in _heartbeat_cell(old, kind="member", now=now)


def test_an_unparseable_heartbeat_is_shown_not_guessed_at() -> None:
    from cabal.cli.main import _heartbeat_cell

    assert _heartbeat_cell("not-a-date", kind="session") == "not-a-date"


def test_a_missing_heartbeat_renders_as_a_dash() -> None:
    from cabal.cli.main import _heartbeat_cell

    assert _heartbeat_cell(None, kind="session") == "—"
