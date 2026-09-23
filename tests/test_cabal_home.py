"""Where cabal looks for identity.toml when nobody said (#211).

The working-directory default is how a live master key (#196) and an operator's
personal profile (#206) were committed to this repo. Both were the same
structural invitation, so the fix is structural — and the compatibility half
matters more than the new default: an existing install whose state sits in its
checkout must keep working, in place, without a migration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cabal.config.home import (
    HOME_ENV,
    IDENTITY_FILENAME,
    cabal_home,
    resolve_identity_path,
)


def test_home_defaults_under_the_user(monkeypatch):
    monkeypatch.delenv(HOME_ENV, raising=False)
    assert cabal_home() == Path.home() / ".cabal"


def test_home_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "elsewhere"))
    assert cabal_home() == tmp_path / "elsewhere"


def test_home_is_resolved_at_call_time(monkeypatch, tmp_path):
    """Not captured at import — a second identity sets the env in-process."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "one"))
    first = cabal_home()
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "two"))
    assert cabal_home() != first


def test_an_explicit_path_is_used_even_when_absent(tmp_path):
    """A caller who names a file gets an error about THAT file, not a fallback.

    The container mounts /app/identity.toml explicitly; silently reading
    something else because the mount failed would be the worst of both.
    """
    named = tmp_path / "nowhere" / "custom.toml"
    assert resolve_identity_path(named) == named


def test_an_existing_cwd_identity_still_wins(tmp_path):
    """THE COMPATIBILITY CASE. Every install predating #211 keeps working."""
    (tmp_path / IDENTITY_FILENAME).write_text("[cabal]\n")
    assert resolve_identity_path(None, cwd=tmp_path) == tmp_path / IDENTITY_FILENAME


def test_an_empty_cwd_falls_through_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_identity_path(None, cwd=empty) == tmp_path / "home" / IDENTITY_FILENAME


def test_a_populated_working_directory_is_never_silently_ignored(tmp_path, monkeypatch):
    """The failure this ordering exists to prevent.

    If the home default won unconditionally, an operator with personas and a
    database in their checkout would start a cabal that found neither — no
    error, no personas, an empty store, and nothing naming the cause.
    """
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / IDENTITY_FILENAME).write_text("[cabal]\n")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / IDENTITY_FILENAME).write_text("[cabal]\n# the operator's real config\n")

    chosen = resolve_identity_path(None, cwd=checkout)
    assert chosen == checkout / IDENTITY_FILENAME
    assert "the operator's real config" in chosen.read_text()


@pytest.mark.parametrize("explicit", ["", None])
def test_falsy_explicit_values_mean_unset(explicit, tmp_path, monkeypatch):
    """The CLI passes "" for "not given"; it must not resolve to Path("")."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    empty = tmp_path / "empty"
    empty.mkdir()
    resolved = resolve_identity_path(explicit or None, cwd=empty)
    assert resolved == tmp_path / "home" / IDENTITY_FILENAME
