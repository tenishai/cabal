"""cabal#92 — a changed master key must be diagnosable and recoverable.

Each test pins one link in the chain that made the original incident require
SQLite surgery: the error had no cause, the log had no message, and there was
no command that undid it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
import structlog.testing
from cryptography.fernet import Fernet

from cabal.core.auth.keys import create_signing_key, load_active_signing_key
from cabal.core.auth.master_key import (
    MasterKeyMismatchError,
    describe_master_key_source,
    resolve_master_key,
)
from cabal.core.auth.reset import ResetError, backup_database, reset_auth, sqlite_path_from_url
from cabal.core.db import make_engine, make_sessionmaker, open_session
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base


@pytest_asyncio.fixture
async def sessionmaker():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest.mark.asyncio
async def test_wrong_master_key_names_the_cause(sessionmaker) -> None:
    """The failure must say WHICH problem it is, not raise InvalidToken.

    Before the fix this surfaced as a bare `cryptography.fernet.InvalidToken`
    from three frames deep, indistinguishable from a corrupted row.
    """
    wrote_it = Fernet.generate_key()
    async with open_session(sessionmaker) as session:
        await create_signing_key(session, master_key=wrote_it)

    other_key = Fernet.generate_key()
    assert other_key != wrote_it
    async with open_session(sessionmaker) as session:
        with pytest.raises(MasterKeyMismatchError) as exc:
            await load_active_signing_key(session, master_key=other_key)
    assert "cannot decrypt" in str(exc.value)


@pytest.mark.asyncio
async def test_the_right_key_still_loads(sessionmaker) -> None:
    """Guards the guard: the new except must not swallow the success path."""
    key = Fernet.generate_key()
    async with open_session(sessionmaker) as session:
        created = await create_signing_key(session, master_key=key)
    async with open_session(sessionmaker) as session:
        loaded = await load_active_signing_key(session, master_key=key)
    assert loaded is not None
    assert loaded.kid == created.kid


def test_two_disagreeing_key_sources_warn(tmp_path) -> None:
    """The env var wins silently today; that silence is how #92 Mode A starts."""
    on_disk = tmp_path / ".master_key"
    on_disk.write_bytes(Fernet.generate_key())
    env_key = Fernet.generate_key().decode()

    # capture_logs records structlog events directly — the global logging
    # config does not route them to caplog.
    with structlog.testing.capture_logs() as logs:
        resolved = resolve_master_key(
            env={"CABAL_MASTER_KEY": env_key}, dev_fallback_path=on_disk
        )

    assert resolved == env_key.encode()
    assert any(entry["event"] == "master_key.sources_differ" for entry in logs)


def test_matching_sources_do_not_warn(tmp_path) -> None:
    """Only a DISAGREEMENT is worth a warning — the same key twice is fine."""
    key = Fernet.generate_key().decode()
    on_disk = tmp_path / ".master_key"
    on_disk.write_text(key)

    with structlog.testing.capture_logs() as logs:
        resolve_master_key(env={"CABAL_MASTER_KEY": key}, dev_fallback_path=on_disk)

    assert not [e for e in logs if e["event"] == "master_key.sources_differ"]


def test_key_source_is_named(tmp_path) -> None:
    assert "CABAL_MASTER_KEY" in describe_master_key_source(
        env={"CABAL_MASTER_KEY": "x"}
    )
    assert str(tmp_path) in describe_master_key_source(
        env={}, dev_fallback_path=tmp_path / ".master_key"
    )


def _make_db(path: Path, *, secrets: int = 0, keys: int = 1) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE signing_keys (kid TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE secrets (name TEXT PRIMARY KEY, value_encrypted BLOB)")
    for i in range(keys):
        conn.execute("INSERT INTO signing_keys VALUES (?)", (f"cabal-{i}",))
    for i in range(secrets):
        conn.execute("INSERT INTO secrets VALUES (?, ?)", (f"s{i}", b"x"))
    conn.commit()
    conn.close()


def test_reset_clears_keys_and_token_but_keeps_secrets(tmp_path) -> None:
    """The asymmetry IS the feature — secrets are the unrecoverable half."""
    db = tmp_path / "cabal.db"
    _make_db(db, secrets=3, keys=2)
    token = tmp_path / "token"
    token.write_text("stale-jwt")

    report = reset_auth(f"sqlite:///{db}", token_path=token)

    assert report.signing_keys_deleted == 2
    assert report.secrets_purged == 0
    assert report.secrets_remaining == 3
    assert report.token_removed is True
    assert not token.exists()

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM signing_keys").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 3
    conn.close()


def test_purge_secrets_is_opt_in(tmp_path) -> None:
    db = tmp_path / "cabal.db"
    _make_db(db, secrets=3)

    report = reset_auth(f"sqlite:///{db}", purge_secrets=True)

    assert report.secrets_purged == 3
    assert report.secrets_remaining == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    conn.close()


def test_reset_backs_up_before_deleting(tmp_path) -> None:
    """A backup that does not contain the deleted rows is not a backup."""
    db = tmp_path / "cabal.db"
    _make_db(db, secrets=1, keys=2)

    report = reset_auth(f"sqlite:///{db}")

    assert report.backup_path.exists()
    conn = sqlite3.connect(report.backup_path)
    assert conn.execute("SELECT COUNT(*) FROM signing_keys").fetchone()[0] == 2
    conn.close()


def test_backup_captures_unflushed_wal_writes(tmp_path) -> None:
    """`cp` would miss these rows; sqlite3's backup API does not."""
    db = tmp_path / "cabal.db"
    _make_db(db)
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("INSERT INTO signing_keys VALUES ('in-wal')")
    live.commit()

    backup = backup_database(db)

    conn = sqlite3.connect(backup)
    kids = {r[0] for r in conn.execute("SELECT kid FROM signing_keys")}
    conn.close()
    live.close()
    assert "in-wal" in kids


def test_non_sqlite_url_is_refused_not_guessed(tmp_path) -> None:
    with pytest.raises(ResetError, match="only supports SQLite"):
        sqlite_path_from_url("postgresql+asyncpg://user@host/cabal")


def test_missing_database_is_an_error(tmp_path) -> None:
    with pytest.raises(ResetError, match="no database"):
        reset_auth(f"sqlite:///{tmp_path / 'nope.db'}")


@pytest.mark.asyncio
async def test_startup_failure_is_logged_not_silent(tmp_path, monkeypatch) -> None:
    """The whole reason #92 cost a day: uvicorn ate the traceback.

    #77 wrapped migrations in log-and-reraise; a mismatched key fails AFTER
    that, so `cbl daemon up` exited with status 3 and a log that just stopped.
    Boot once to write a signing key, then boot again under a different key and
    assert the operator is told what happened and what to do.
    """
    from cabal.core.app import create_app

    db = tmp_path / "cabal.db"
    db_url = f"sqlite+aiosqlite:///{db}"
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("CABAL_MASTER_KEY", Fernet.generate_key().decode())
    app = create_app(db_url=db_url, config_path="absent.toml", print_bootstrap_code=False)
    async with app.router.lifespan_context(app):
        pass

    monkeypatch.setenv("CABAL_MASTER_KEY", Fernet.generate_key().decode())
    app2 = create_app(db_url=db_url, config_path="absent.toml", print_bootstrap_code=False)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(MasterKeyMismatchError):
            async with app2.router.lifespan_context(app2):
                pass

    mismatch = [e for e in logs if e["event"] == "cabal.master_key_mismatch"]
    assert mismatch, [e["event"] for e in logs]
    assert str(db) in mismatch[0]["db_url"]
    assert "CABAL_MASTER_KEY" in mismatch[0]["key_source"]
    assert "reset-auth" in mismatch[0]["msg"]
