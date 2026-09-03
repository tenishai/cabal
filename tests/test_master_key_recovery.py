"""cabal#92 — a changed master key must be diagnosable and recoverable.

Each test pins one link in the chain that made the original incident require
SQLite surgery: the error had no cause, the log had no message, and there was
no command that undid it.
"""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
import structlog.testing
from cryptography.fernet import Fernet

from cabal.core.auth.keys import create_signing_key, load_active_signing_key
from cabal.core.auth.master_key import (
    EncryptedRowDamagedError,
    MasterKeyMismatchError,
    describe_master_key_source,
    is_wellformed_fernet_token,
    resolve_master_key,
)
from cabal.core.auth.reset import ResetError, backup_database, reset_auth, sqlite_path_from_url
from cabal.core.db import make_engine, make_sessionmaker, open_session
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base, Secret, SigningKey


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
    """Real tables from `Base.metadata`, not hand-rolled CREATE statements.

    reset.py resolves its table names from the models; building the fixture
    from a literal schema here would let a `__tablename__` rename break the
    recovery command while these tests stayed green (Prudence, #92 review).
    """
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for i in range(keys):
            conn.execute(
                sa.text(
                    f"INSERT INTO {SigningKey.__tablename__} "
                    "(kid, public_jwk_json, private_key_pem_encrypted, status) "
                    "VALUES (:kid, '{}', :blob, 'active')"
                ),
                {"kid": f"cabal-{i}", "blob": b"x"},
            )
        for i in range(secrets):
            conn.execute(
                sa.text(
                    f"INSERT INTO {Secret.__tablename__} (name, value_encrypted) "
                    "VALUES (:name, :blob)"
                ),
                {"name": f"s{i}", "blob": b"x"},
            )
    engine.dispose()


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
    live.execute(
        f"INSERT INTO {SigningKey.__tablename__} "
        "(kid, public_jwk_json, private_key_pem_encrypted, status) "
        "VALUES ('in-wal', '{}', X'78', 'active')"
    )
    live.commit()

    backup = backup_database(db)

    conn = sqlite3.connect(backup)
    kids = {r[0] for r in conn.execute(f"SELECT kid FROM {SigningKey.__tablename__}")}
    conn.close()
    live.close()
    assert "in-wal" in kids


def test_non_sqlite_url_is_refused_not_guessed() -> None:
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


# --- Prudence's findings on the #92 review, each pinned by a test ------------


def test_backup_does_not_widen_permissions(tmp_path) -> None:
    """FINDING 1 (Critical). sqlite3 creates under the umask — 0644 beside a
    0600 database. The encrypted columns survive that, but conversations,
    events and personas are plaintext."""
    db = tmp_path / "cabal.db"
    _make_db(db, secrets=1)
    db.chmod(0o600)

    backup = backup_database(db)

    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_two_resets_in_one_second_keep_both_backups(tmp_path) -> None:
    """FINDING 2. The stamp is second-resolution and sqlite3.backup overwrites
    wholesale, so the second reset replaced the only copy of the deleted rows
    with a snapshot of the already-reset database."""
    db = tmp_path / "cabal.db"
    _make_db(db, keys=2)
    fixed = datetime(2026, 9, 3, 21, 4, 27, tzinfo=UTC)

    first = backup_database(db, now=fixed)
    conn = sqlite3.connect(db)
    conn.execute(f"DELETE FROM {SigningKey.__tablename__}")
    conn.commit()
    conn.close()
    second = backup_database(db, now=fixed)

    assert first != second
    conn = sqlite3.connect(first)
    survived = conn.execute(
        f"SELECT COUNT(*) FROM {SigningKey.__tablename__}"
    ).fetchone()[0]
    conn.close()
    assert survived == 2, "the pre-reset backup must still hold the deleted rows"


def test_locked_database_gives_a_remedy_not_a_traceback(tmp_path) -> None:
    """FINDING 3. A live daemon holds the write lock; the CLI catches only
    ResetError, so sqlite3.OperationalError reached the operator raw."""
    db = tmp_path / "cabal.db"
    _make_db(db)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ResetError, match="Stop the cabal daemon"):
            reset_auth(f"sqlite:///{db}")
    finally:
        holder.close()


def test_damaged_row_is_not_reported_as_a_wrong_key(sessionmaker) -> None:
    """FINDING 4, the one that touched the PR's central claim.

    Fernet raises InvalidToken for a wrong key AND for damaged ciphertext. The
    first version collapsed both into "restore your master key" — advice that
    sends the operator after a key they never lost.
    """
    assert not is_wellformed_fernet_token(b"this is not a fernet token")
    assert not is_wellformed_fernet_token(b"")
    real = Fernet(Fernet.generate_key()).encrypt(b"pem")
    assert is_wellformed_fernet_token(real)
    assert not is_wellformed_fernet_token(real[:16])


@pytest.mark.asyncio
async def test_damaged_ciphertext_raises_its_own_error(sessionmaker) -> None:
    """The end-to-end half of FINDING 4: damage under the CORRECT key."""
    key = Fernet.generate_key()
    async with open_session(sessionmaker) as session:
        created = await create_signing_key(session, master_key=key)

    async with open_session(sessionmaker) as session:
        await session.execute(
            sa.text(
                f"UPDATE {SigningKey.__tablename__} "
                "SET private_key_pem_encrypted = :junk WHERE kid = :kid"
            ),
            {"junk": b"@@ not a fernet token @@", "kid": created.kid},
        )
        await session.commit()

    async with open_session(sessionmaker) as session:
        with pytest.raises(EncryptedRowDamagedError, match="row is damaged"):
            await load_active_signing_key(session, master_key=key)


@pytest.mark.asyncio
async def test_shutdown_failure_is_logged_too(tmp_path, monkeypatch) -> None:
    """FINDING 6. `started` correctly suppressed the misleading
    `startup_failed`, but nothing replaced it — the exception went back to
    uvicorn, which is the swallowing this PR exists to end."""
    from cabal.core.app import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CABAL_MASTER_KEY", Fernet.generate_key().decode())
    app = create_app(
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}",
        config_path="absent.toml",
        print_bootstrap_code=False,
    )
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError, match="boom"):
            async with app.router.lifespan_context(app):
                raise RuntimeError("boom")

    events = [e["event"] for e in logs]
    assert "cabal.shutdown_failed" in events
    assert "cabal.startup_failed" not in events


def test_sources_differ_check_uses_the_default_path(tmp_path, monkeypatch) -> None:
    """FINDING 7. Both original tests passed dev_fallback_path explicitly, so
    nothing covered the default the daemon actually uses."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / ".master_key").write_bytes(Fernet.generate_key())

    with structlog.testing.capture_logs() as logs:
        resolve_master_key(env={"CABAL_MASTER_KEY": Fernet.generate_key().decode()})

    assert any(e["event"] == "master_key.sources_differ" for e in logs)


def test_reset_uses_the_models_table_names(tmp_path) -> None:
    """FINDING 8. reset.py resolving names from the models is what keeps a
    __tablename__ rename from silently breaking recovery."""
    from cabal.core.auth import reset as reset_mod

    assert reset_mod._SIGNING_KEYS == SigningKey.__tablename__
    assert reset_mod._SECRETS == Secret.__tablename__
