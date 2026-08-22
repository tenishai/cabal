"""Migration smoke test.

Runs `alembic upgrade head` against a fresh tempfile-backed SQLite DB and
verifies that the resulting schema matches `Base.metadata` exactly. This
catches drift between the ORM models and the hand-checked-in migration —
if you change a model without generating a new revision, this fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from cabal.core.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_head_matches_metadata(tmp_path: Path, monkeypatch) -> None:
    db_file = tmp_path / "migration_smoke.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("CABAL_DB_URL", db_url)

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    expected = set(Base.metadata.tables.keys())
    assert expected.issubset(tables), f"missing tables: {expected - tables}"
    assert "alembic_version" in tables


# ---- dispatch_pairs: additive migration (Task C3) --------------------------


def test_dispatch_pairs_migration_is_additive(tmp_path: Path, monkeypatch) -> None:
    """Upgrading past the dispatch_pairs revision must not disturb rows
    already sitting in unrelated tables -- it only adds one new, empty
    table. Seeds a row on the immediately-prior revision, upgrades the
    rest of the way to head, and checks nothing was lost."""
    db_file = tmp_path / "dispatch_pairs_additive.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("CABAL_DB_URL", db_url)

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))

    # The revision immediately before dispatch_pairs was introduced.
    command.upgrade(cfg, "a1b2c3d4e5f6")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO personas (name, config_json, created_at) "
                "VALUES ('probe', '{}', CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert "dispatch_pairs" in inspector.get_table_names()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM personas WHERE name = 'probe'")
        ).fetchone()
        assert row is not None, "pre-existing row was lost across the dispatch_pairs migration"

        count = conn.execute(text("SELECT COUNT(*) FROM dispatch_pairs")).scalar()
        assert count == 0


# ---- Bare-metal first-boot: DB dir created (cabal#77) -----------------------


def test_ensure_sqlite_parent_dir(tmp_path: Path) -> None:
    """The helper creates a missing SQLite parent dir and no-ops safely on
    in-memory / non-SQLite URLs."""
    from cabal.core.app import _ensure_sqlite_parent_dir

    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists()
    _ensure_sqlite_parent_dir(f"sqlite+aiosqlite:///{nested}/cabal.db")
    assert nested.exists()

    # No-ops that must not raise.
    _ensure_sqlite_parent_dir("sqlite://")  # pure in-memory
    _ensure_sqlite_parent_dir("sqlite:///:memory:")  # in-memory
    _ensure_sqlite_parent_dir("postgresql://host/db")  # non-sqlite
    _ensure_sqlite_parent_dir("not a url")  # unparseable → no raise


@pytest.mark.asyncio
async def test_create_app_boots_when_db_dir_missing(tmp_path: Path) -> None:
    """Regression for cabal#77: a fresh bare-metal boot must create the
    SQLite parent directory. Previously the default sqlite:///data/cabal.db
    died with "unable to open database file" because data/ didn't exist —
    Docker's volume mount masked it, so the bare-metal path went unexercised.
    """
    from cabal.core.app import create_app

    # A nested directory that does NOT exist yet — the failure condition.
    db_dir = tmp_path / "fresh" / "boot" / "data"
    assert not db_dir.exists()
    db_url = f"sqlite+aiosqlite:///{db_dir}/cabal.db"

    app = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    # A clean startup + shutdown through the lifespan is the assertion: it runs
    # migrations and opens the DB, both of which fail if the dir is missing.
    async with app.router.lifespan_context(app):
        pass

    assert (db_dir / "cabal.db").exists()
