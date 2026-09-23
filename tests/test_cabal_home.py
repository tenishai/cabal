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
    DATA_DIRNAME,
    HOME_ENV,
    IDENTITY_FILENAME,
    cabal_home,
    default_db_url,
    default_master_key_path,
    resolve_identity_path,
    state_root,
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
    # Calls the function with the raw value, so the guard under test is the
    # function's own — not a re-implementation of the CLI's (Prudence r1, #212).
    resolved = resolve_identity_path(explicit, cwd=empty)
    assert resolved == tmp_path / "home" / IDENTITY_FILENAME


@pytest.mark.parametrize("marker", ["cabal.db", ".master_key"])
def test_a_database_or_key_alone_is_an_existing_install(marker, tmp_path, monkeypatch):
    """A checkout that ran without identity.toml still has its state in ./data."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    checkout = tmp_path / "checkout"
    (checkout / DATA_DIRNAME).mkdir(parents=True)
    (checkout / DATA_DIRNAME / marker).write_bytes(b"")
    assert state_root(cwd=checkout) == checkout


def test_someone_elses_data_directory_is_not_an_install(tmp_path, monkeypatch):
    """Prudence r2 probe D: `data/` alone is a common name. Treating it as cabal
    state put the dev master key inside another project's tree."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    project = tmp_path / "project"
    (project / DATA_DIRNAME).mkdir(parents=True)
    (project / DATA_DIRNAME / "notebook.csv").write_text("a,b\n")
    assert state_root(cwd=project) == tmp_path / "home"
    assert default_master_key_path(cwd=project) == tmp_path / "home" / DATA_DIRNAME / ".master_key"


def test_the_key_disagreement_check_does_not_announce(tmp_path, monkeypatch):
    """Prudence r2 nit: every container has /app/identity.toml in its cwd and sets
    CABAL_MASTER_KEY, so the check that compares against the on-disk default ran
    state_root() and logged move-your-state advice that does not apply there."""
    from cryptography.fernet import Fernet

    import cabal.config.home as home
    from cabal.core.auth.master_key import resolve_master_key

    monkeypatch.setattr(home, "_hinted", set())
    logged = []
    monkeypatch.setattr(home.log, "info", lambda event, **kw: logged.append(event))
    (tmp_path / IDENTITY_FILENAME).write_text("[cabal]\n")
    monkeypatch.chdir(tmp_path)

    resolve_master_key(env={"CABAL_MASTER_KEY": Fernet.generate_key().decode()})
    assert logged == []


def test_every_default_shares_one_root(tmp_path, monkeypatch):
    """The split Prudence found in r1: identity moved, cabal.db and the key did not."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    for cwd, root in (
        (tmp_path / "empty", tmp_path / "home"),
        (tmp_path / "legacy", tmp_path / "legacy"),
    ):
        cwd.mkdir()
        if cwd.name == "legacy":
            (cwd / IDENTITY_FILENAME).write_text("[cabal]\n")
        assert resolve_identity_path(None, cwd=cwd).parent == root
        assert default_db_url(cwd=cwd) == f"sqlite+aiosqlite:///{root / DATA_DIRNAME / 'cabal.db'}"
        assert default_db_url(cwd=cwd, sync=True).startswith("sqlite:///")
        assert default_master_key_path(cwd=cwd) == root / DATA_DIRNAME / ".master_key"


def test_the_upgrade_hint_names_everything_that_has_to_move(tmp_path, monkeypatch, capsys):
    """r1 found the hint said "move it" (one file) and offered CABAL_HOME, which
    has no effect while ./identity.toml exists. Moving only the identity file
    moves data_dir with it and leaves the database behind."""
    import cabal.config.home as home

    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    monkeypatch.setattr(home, "_hinted", set())
    logged = []
    monkeypatch.setattr(home.log, "info", lambda event, **kw: logged.append((event, kw)))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / IDENTITY_FILENAME).write_text("[cabal]\n")

    state_root(cwd=checkout)
    state_root(cwd=checkout)

    assert len(logged) == 1, "the hint should appear once per directory, not per call"
    note = logged[0][1]["note"]
    assert IDENTITY_FILENAME in note and f"{DATA_DIRNAME}/" in note and "BOTH" in note


async def test_a_fresh_install_writes_nothing_into_the_checkout(tmp_path, monkeypatch):
    """End to end through the real lifespan: the probe Prudence ran by hand in r1.

    Nothing configured — no CABAL_DB_URL, no CABAL_MASTER_KEY, no --config —
    from an empty working directory. Every file cabal creates must land under
    CABAL_HOME, and the working directory must stay empty.
    """
    from cabal.core.app import create_app

    home_dir = tmp_path / "home"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home_dir))
    monkeypatch.delenv("CABAL_DB_URL", raising=False)
    monkeypatch.delenv("CABAL_MASTER_KEY", raising=False)
    monkeypatch.chdir(checkout)

    app = create_app(agent="all", allow_dev_master_key=True, print_bootstrap_code=False)
    async with app.router.lifespan_context(app):
        pass

    assert list(checkout.iterdir()) == [], f"cabal wrote into the checkout: {list(checkout.rglob('*'))}"
    assert (home_dir / DATA_DIRNAME / "cabal.db").exists()
    assert (home_dir / DATA_DIRNAME / ".master_key").exists()
