"""The CLI must outlast the daemon it waits on (PR #123 review).

CABAL_DISPATCH_TIMEOUT_S widened the daemon's federated read ceiling, but
`cbl`'s own client stayed pinned at 30s — so following the README's advice to
raise it for a slow peer produced a CLI that aborted at 30s while the daemon
kept working, which is the exact failure the knob exists to prevent.
"""

from __future__ import annotations

import httpx
import pytest

from cabal.cli import client as cli_client
from cabal.core.dispatch_timeout import (
    CLI_DISPATCH_HEADROOM_FACTOR,
    DISPATCH_CONNECT_TIMEOUT_S,
    cli_dispatch_timeout,
    dispatch_timeout,
)


def test_cli_ceiling_clears_the_daemon_ceiling(monkeypatch):
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "300")
    assert cli_dispatch_timeout().read > dispatch_timeout().read


def test_cli_ceiling_tracks_the_env_var(monkeypatch):
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "300")
    assert cli_dispatch_timeout().read == 300.0 * CLI_DISPATCH_HEADROOM_FACTOR


def test_default_is_unchanged_for_the_daemon(monkeypatch):
    monkeypatch.delenv("CABAL_DISPATCH_TIMEOUT_S", raising=False)
    assert dispatch_timeout().read == 30.0


def test_cli_connect_stays_short(monkeypatch):
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "600")
    assert cli_dispatch_timeout().connect == DISPATCH_CONNECT_TIMEOUT_S


def test_admin_calls_keep_the_short_default(monkeypatch):
    """A hung daemon must not make `cbl agents list` hang for minutes."""
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "600")
    with cli_client.make_client() as c:
        assert c.timeout.read == 30.0


def test_make_client_honours_an_explicit_timeout():
    with cli_client.make_client(timeout=httpx.Timeout(123.0)) as c:
        assert c.timeout.read == 123.0


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "nan", "1e400"])
def test_cli_rejects_what_the_daemon_rejects(monkeypatch, bad):
    """One resolver: the CLI cannot silently accept a value the daemon aborts on."""
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", bad)
    with pytest.raises(ValueError):
        cli_dispatch_timeout()


def test_the_unpatched_cli_was_genuinely_defeated(monkeypatch):
    """Teeth: pin the bug, not just the fix.

    The old client was `httpx.Timeout(30.0)` unconditionally. At a raised
    daemon ceiling that is BELOW the daemon's own read timeout, so the CLI
    aborts first. Fails if make_client stops honouring the dispatch ceiling.
    """
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "300")
    old_behaviour = httpx.Timeout(30.0)
    assert old_behaviour.read < dispatch_timeout().read
    with cli_client.make_client(timeout=cli_dispatch_timeout()) as c:
        assert c.timeout.read > dispatch_timeout().read
