"""`cbl archivist purge`: hard-delete every version of an entry (#351).

Store-level, no liminal import, so these run in unit CI.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cabal.archivist import ArchivistStore, Entry
from cabal.archivist.purge import purge
from cabal.cli.main import app

runner = CliRunner()

SECRET = "zqxsecretvalue"
SECRET_DETAIL = "zqxleakedmailbox"


def _seed(db: Path) -> ArchivistStore:
    store = ArchivistStore(db)
    store.publish(Entry(resource_name="leak", summary=f"first {SECRET}", entry_type="fact"))
    # A second version supersedes the first; both rows stay in the table.
    store.publish(
        Entry(
            resource_name="leak",
            summary=f"second {SECRET}",
            entry_type="fact",
            details=SECRET_DETAIL,
            gotchas="a gotcha",
        ),
        supersede=True,
    )
    store.publish(Entry(resource_name="leak", summary="a decision", entry_type="decision"))
    store.publish(Entry(resource_name="keep", summary="unrelated fact", entry_type="fact"))
    return store


def _rows(db: Path, sql: str, *args) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _indexed_terms(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab "
            "USING fts5vocab(main, memory_entries_fts, 'row')"
        )
        return {t for (t,) in conn.execute("SELECT term FROM temp.vocab")}
    finally:
        conn.close()


def _file_bytes(db: Path) -> bytes:
    data = db.read_bytes()
    wal = db.with_name(db.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


def test_dry_run_lists_every_version_and_changes_nothing(tmp_path):
    db = tmp_path / "a.db"
    _seed(db)
    before = _rows(db, "SELECT id, status FROM memory_entries ORDER BY id")

    result = purge(db, "leak")

    assert not result.applied
    assert sorted(e.status for e in result.entries) == ["active", "active", "superseded"]
    assert sorted(e.entry_type for e in result.entries) == ["decision", "fact", "fact"]
    assert result.relations == 1  # the SUPERSEDES edge
    assert _rows(db, "SELECT id, status FROM memory_entries ORDER BY id") == before
    assert SECRET in _indexed_terms(db)


def test_apply_removes_rows_relations_and_index_terms(tmp_path):
    db = tmp_path / "a.db"
    store = _seed(db)
    assert SECRET in _indexed_terms(db)
    assert SECRET_DETAIL in _indexed_terms(db)

    result = purge(db, "leak", apply=True)

    assert result.applied
    assert len(result.entries) == 3
    assert _rows(db, "SELECT COUNT(*) FROM memory_entries WHERE resource_name='leak'") == [(0,)]
    assert _rows(db, "SELECT COUNT(*) FROM memory_relations") == [(0,)]
    terms = _indexed_terms(db)
    assert SECRET not in terms
    assert SECRET_DETAIL not in terms
    assert "gotcha" not in terms
    # The other entry is untouched and still found through the index.
    assert [e.resource_name for e in store.search("unrelated")] == ["keep"]


def test_apply_leaves_no_text_in_the_database_file(tmp_path):
    db = tmp_path / "a.db"
    _seed(db)
    # Control: without the purge the text is in the file, so the check below means something.
    assert SECRET.encode() in _file_bytes(db)

    purge(db, "leak", apply=True)

    data = _file_bytes(db)
    assert SECRET.encode() not in data
    assert SECRET_DETAIL.encode() not in data


def test_apply_removes_relations_pointing_at_the_entry(tmp_path):
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    store.publish(Entry(resource_name="leak", summary="old", entry_type="fact"))
    # "keep" supersedes nothing, so add an edge into "leak" by hand: a purge
    # must remove edges in both directions.
    store.publish(Entry(resource_name="keep", summary="other", entry_type="fact"))
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memory_relations (src_id, dst_id, relation_type, created_at) "
        "SELECT k.id, l.id, 'RELATED', 'now' FROM memory_entries k, memory_entries l "
        "WHERE k.resource_name='keep' AND l.resource_name='leak'"
    )
    conn.commit()
    conn.close()

    result = purge(db, "leak", apply=True)

    assert result.relations == 1
    assert _rows(db, "SELECT COUNT(*) FROM memory_relations") == [(0,)]


def test_name_match_is_exact(tmp_path):
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    store.publish(Entry(resource_name="leak-2", summary="not this one", entry_type="fact"))

    result = purge(db, "leak", apply=True)

    assert result.entries == []
    assert _rows(db, "SELECT resource_name FROM memory_entries") == [("leak-2",)]


def test_apply_with_no_match_still_runs_cleanup(tmp_path):
    db = tmp_path / "a.db"
    _seed(db)
    result = purge(db, "nothing-here", apply=True)
    assert result.entries == []
    assert result.relations == 0


def test_missing_database_is_refused_not_created(tmp_path):
    db = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        purge(db, "leak")
    assert not db.exists()


def test_cli_defaults_to_dry_run(tmp_path):
    db = tmp_path / "a.db"
    _seed(db)

    out = runner.invoke(app, ["archivist", "purge", "leak", "--db", str(db)])

    assert out.exit_code == 0, out.output
    assert "would remove 3 entries and 1 relations for 'leak'" in out.output
    assert "--apply" in out.output
    assert SECRET not in out.output
    assert _rows(db, "SELECT COUNT(*) FROM memory_entries WHERE resource_name='leak'") == [(3,)]


def test_cli_apply_reads_the_env_database(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    _seed(db)
    monkeypatch.setenv("CABAL_ARCHIVIST_DB", str(db))

    out = runner.invoke(app, ["archivist", "purge", "leak", "--apply"])

    assert out.exit_code == 0, out.output
    assert "removed 3 entries and 1 relations for 'leak'" in out.output
    assert _rows(db, "SELECT COUNT(*) FROM memory_entries WHERE resource_name='leak'") == [(0,)]


def test_cli_without_a_database_exits_2(tmp_path, monkeypatch):
    monkeypatch.delenv("CABAL_ARCHIVIST_DB", raising=False)
    out = runner.invoke(app, ["archivist", "purge", "leak"])
    assert out.exit_code == 2
    out = runner.invoke(app, ["archivist", "purge", "leak", "--db", str(tmp_path / "no.db")])
    assert out.exit_code == 2
