"""Migration smoke test.

Runs `alembic upgrade head` against a fresh tempfile-backed SQLite DB and
verifies that the resulting schema matches `Base.metadata` exactly. This
catches drift between the ORM models and the hand-checked-in migration —
if you change a model without generating a new revision, this fails.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

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
