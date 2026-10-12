"""cabal#79 — migrations must ship with the package, not with the repo.

`_run_migrations` located `alembic.ini` and `migrations/` via
`REPO_ROOT = Path(__file__).parents[3]`, which under a non-editable
`pip install tenish-cabal` points into `site-packages`, where neither exists. It was
mitigated by `CABAL_ALEMBIC_INI` / `CABAL_ALEMBIC_MIGRATIONS`, which the
Dockerfile used to set, so a bare `pip install tenish-cabal` + run-server on a clean
machine broke unless the operator knew to set env vars nobody documents. The
Dockerfile no longer sets either: the #79 review found that the override takes
precedence, so leaving it pointed at /app/migrations kept the container off
the packaged path entirely.

The issue names a prerequisite before the packaging change: there was NO test
coverage for `_run_migrations` at all. These are that coverage, written first
so the move is verifiable rather than hopeful.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from cabal.core.app import _migrations_dir, _run_migrations


def test_the_migrations_directory_resolves_inside_the_package() -> None:
    """THE ACTUAL #79 PROPERTY. Not "a path exists" but "the path is inside the
    installed package", which is the only thing that survives a non-editable
    install. A resolution that walks UP from the module lands in site-packages
    and finds nothing."""
    d = _migrations_dir()
    assert d.is_dir(), f"migrations directory does not exist: {d}"

    import cabal

    package_root = Path(cabal.__file__).resolve().parent
    assert package_root in d.resolve().parents or d.resolve().parent == package_root, (
        f"migrations at {d} are not inside the cabal package at {package_root} — "
        "a non-editable install will not ship them"
    )


def test_the_shipped_migrations_are_complete() -> None:
    """env.py and at least one revision. A directory that exists but is empty
    would satisfy the test above and still fail at `upgrade head`."""
    d = _migrations_dir()
    assert (d / "env.py").is_file(), "alembic cannot run without env.py"
    revisions = list((d / "versions").glob("*.py"))
    assert revisions, "no migration revisions shipped"


def test_run_migrations_brings_a_fresh_database_to_head(tmp_path: Path) -> None:
    """END TO END, and the reason the other two are not enough: they assert
    about paths, this one runs alembic and looks at the result."""
    db_file = tmp_path / "fresh.db"
    _run_migrations(f"sqlite:///{db_file}")

    assert db_file.exists(), "migrations did not create the database"
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "alembic_version" in tables, "alembic did not stamp a version"
    # A table every revision path must produce; named rather than counted so
    # the assertion says what it wants.
    assert "conversations" in tables, f"schema is not at head; got {sorted(tables)}"


def test_an_env_override_still_wins(tmp_path: Path, monkeypatch) -> None:
    """The escape hatch must keep working, even though nothing in this repo
    sets it any more.

    The Dockerfile DID set CABAL_ALEMBIC_MIGRATIONS until the #79 review -- and
    that was the bug, because the override wins and kept the container off the
    packaged path. Removing it there does not make the override dead config: a
    deployment that relocates migrations still depends on it, and silently
    dropping support would break that deployment on upgrade."""
    monkeypatch.setenv("CABAL_ALEMBIC_MIGRATIONS", str(tmp_path / "nowhere"))
    with pytest.raises(Exception):
        _run_migrations(f"sqlite:///{tmp_path / 'x.db'}")
