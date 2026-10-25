"""Git-syncable Archivist corpus (Phase 2): export <-> materialize, conflict-free merge.

Store-level only — no liminal (runs in the no-siblings CI job).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cabal.archivist import ArchivistStore, Entry
from cabal.archivist.gitsync import export_corpus, materialize_corpus


def _entry(summary: str, *, resource: str = "r", etype: str = "decision") -> Entry:
    return Entry(resource_name=resource, summary=summary, entry_type=etype)


def _rows(db_path: Path) -> set[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return {
            (r["summary"], r["status"])
            for r in conn.execute("SELECT summary, status FROM memory_entries")
        }
    finally:
        conn.close()


def test_export_materialize_round_trip(tmp_path: Path) -> None:
    src = ArchivistStore(tmp_path / "src.db")
    src.publish(_entry("v1", resource="auth"))
    src.publish(_entry("v2", resource="auth"), supersede=True)  # supersedes v1
    src.publish(_entry("standalone", resource="other"))

    corpus = tmp_path / "corpus"
    rep = export_corpus(src, corpus)
    assert rep["entries"] == 3
    assert rep["markers"] == 1
    # files are content-addressed, status not in the file
    assert len(list((corpus / "entries").glob("*.json"))) == 3
    assert len(list((corpus / "supersedes").glob("*.json"))) == 1

    mat = materialize_corpus(corpus, tmp_path / "mat.db")
    rows = _rows(tmp_path / "mat.db")
    assert ("v1", "superseded") in rows  # status derived from the marker
    assert ("v2", "active") in rows
    assert ("standalone", "active") in rows
    # search is active-only → only the latest surfaces
    assert {e.summary for e in mat.search("v2")} == {"v2"}
    assert mat.search("v1") == []


def test_merge_two_machines_converges(tmp_path: Path) -> None:
    """Union the corpora of two independent machines (a 'git merge') and
    materialize — supersession survives, everyone's entries are present."""
    a = ArchivistStore(tmp_path / "a.db")
    a.publish(_entry("jwt alpha", resource="auth"))
    a.publish(_entry("jwt beta", resource="auth"), supersede=True)  # A retires alpha
    a.publish(_entry("ci on linux", resource="ci"))

    b = ArchivistStore(tmp_path / "b.db")
    b.publish(_entry("ci nightly", resource="ci"))
    b.publish(_entry("postgres 16", resource="db"))

    merged = tmp_path / "merged"
    export_corpus(a, merged)
    export_corpus(b, merged)  # union into the same dir (no deletes, pure adds)

    materialize_corpus(merged, tmp_path / "m.db")
    rows = _rows(tmp_path / "m.db")
    assert ("jwt alpha", "superseded") in rows  # A's supersession held across the merge
    assert ("jwt beta", "active") in rows
    for s in ("ci on linux", "ci nightly", "postgres 16"):
        assert (s, "active") in rows


def test_identical_entry_from_two_machines_is_one_file(tmp_path: Path) -> None:
    """Same content distilled on two machines → same content hash → one file."""
    a = ArchivistStore(tmp_path / "a.db")
    a.publish(_entry("the very same lesson", resource="x"))
    b = ArchivistStore(tmp_path / "b.db")
    b.publish(_entry("the very same lesson", resource="x"))

    merged = tmp_path / "merged"
    export_corpus(a, merged)
    export_corpus(b, merged)
    assert len(list((merged / "entries").glob("*.json"))) == 1  # idempotent, conflict-free


def test_reasserted_content_stays_active_after_a_round_trip(tmp_path: Path) -> None:
    """#367: publish A, supersede with B, re-publish A with supersede=True. SQLite
    holds A active and B superseded; export + materialize used to leave both
    superseded, because the markers point both ways between the two hashes."""
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    store.publish(_entry("mounted at a"))
    store.publish(_entry("mounted at b"), supersede=True)
    store.publish(_entry("mounted at a"), supersede=True)  # same process: #353 fix

    export_corpus(store, tmp_path / "corpus")
    view = tmp_path / "view.db"
    materialize_corpus(tmp_path / "corpus", view)

    assert _rows(view) == {("mounted at a", "active"), ("mounted at b", "superseded")}


def test_a_marker_without_at_still_retires_its_target() -> None:
    from cabal.archivist.gitsync import _superseded_hashes

    assert _superseded_hashes([("b", "a", None)]) == {"a"}
    assert _superseded_hashes([("b", "a", "2026-01-01"), ("a", "b", "2026-01-02")]) == {"b"}
