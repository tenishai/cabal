"""techne -> Archivist promotion bridge (cabal Phase 1).

Builds a minimal techne `knowledge` table via raw SQL (no techne dep needed)
and promotes it into a local Archivist. Store-level only — no liminal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cabal.archivist import ArchivistStore, Entry
from cabal.archivist.promote import promote_techne_knowledge


def test_entry_from_row_preserves_zero_importance() -> None:
    """A stored importance of 0.0 must read back as 0.0, not the 1.0 default
    (Prudence review: resource:falsy-default-bug, read side)."""
    e = Entry.from_row(
        {
            "id": "x",
            "resource_name": "r",
            "summary": "s",
            "entry_type": "fact",
            "importance": 0.0,
        }
    )
    assert e.importance == 0.0


def test_promote_preserves_zero_confidence_as_importance(tmp_path: Path) -> None:
    """A legitimate 0.0 techne confidence must map to importance 0.0 end-to-end —
    not be corrupted to the 1.0 default on the way in OR back out (Lesson 2)."""
    tdb = _techne_db(
        tmp_path / "techne.db",
        [{"type": "fact", "content": "an uncertain claim", "tags": "", "conf": 0.0}],
    )
    store = ArchivistStore(tmp_path / "arch.db")
    rep = promote_techne_knowledge(tdb, store, written_by="Prudence")
    assert rep.new == 1
    hits = store.search("uncertain claim")
    assert hits
    assert hits[0].importance == 0.0  # not 1.0


def _techne_db(path: Path, rows: list[dict], *, with_status: bool = True) -> Path:
    """Build a minimal techne `knowledge` table.

    `with_status=False` reproduces the PRE-status schema of an older techne store,
    which promote must still read rather than raising OperationalError.
    """
    status_col = ",\n            status TEXT NOT NULL DEFAULT 'current'" if with_status else ""
    conn = sqlite3.connect(str(path))
    conn.execute(
        f"""CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            tags TEXT,
            confidence_numeric REAL NOT NULL DEFAULT 0.6,
            superseded_by INTEGER{status_col}
        )"""
    )
    cols = "type, content, tags, confidence_numeric, superseded_by"
    ph = "?, ?, ?, ?, ?"
    for r in rows:
        vals = [r["type"], r["content"], r.get("tags"), r.get("conf", 0.6), r.get("superseded_by")]
        if with_status:
            vals.append(r.get("status", "current"))
        conn.execute(
            f"INSERT INTO knowledge ({cols}{', status' if with_status else ''}) "
            f"VALUES ({ph}{', ?' if with_status else ''})",
            vals,
        )
    conn.commit()
    conn.close()
    return path


def test_promote_skips_uncertain_knowledge(tmp_path: Path) -> None:
    """`uncertain` is techne's doubt-cascade status (_cascade_uncertain), set when
    an entry's supporting knowledge is contradicted, and carrying a 0.5 confidence
    penalty. The first version of this filter was a DENYLIST of `archived`, so
    these were published to the unretractable shared corpus — and at full
    un-penalised importance, since promote reads raw confidence_numeric rather
    than techne's effective confidence. Selection is now an allowlist of
    `current` (Prudence, PR #115).
    """
    tdb = _techne_db(
        tmp_path / "techne.db",
        [
            {"type": "fact", "content": "settled and shareable", "tags": "keep"},
            {
                "type": "fact",
                "content": "techne itself has flagged this as doubtful",
                "tags": "doubt",
                "status": "uncertain",
            },
        ],
    )
    store = ArchivistStore(tmp_path / "arch.db")
    rep = promote_techne_knowledge(tdb, store, written_by="Prudence")

    assert rep.read == 1, "an uncertain row must not even be read as a candidate"
    assert store.search("settled and shareable")
    assert store.search("doubtful") == [], "uncertain must not reach a write-only corpus"


def test_promote_treats_null_status_as_current(tmp_path: Path) -> None:
    """Fail closed on unknown VALUES, fail open on an absent CONCEPT.

    `status = 'current'` is NULL-propagating, so a store whose column is
    NULLABLE and unset would promote NOTHING AT ALL — silently, which is the
    exact failure class this change exists to remove. A NULL means the row
    predates or ignores the concept, the same situation as the absent column.

    Builds the schema inline rather than via `_techne_db`, which declares
    `status TEXT NOT NULL DEFAULT 'current'` (matching techne itself) and so
    cannot express the pathological store this guards against.
    """
    tdb = tmp_path / "nullable.db"
    conn = sqlite3.connect(str(tdb))
    conn.execute(
        """CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            tags TEXT,
            confidence_numeric REAL NOT NULL DEFAULT 0.6,
            superseded_by INTEGER,
            status TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO knowledge (type, content, tags, status) VALUES (?, ?, ?, NULL)",
        ("fact", "row from a store with a nullable status", "x"),
    )
    conn.commit()
    conn.close()

    store = ArchivistStore(tmp_path / "arch.db")
    rep = promote_techne_knowledge(tdb, store, written_by="Prudence")
    assert rep.new == 1, "a NULL status must not silently suppress everything"
    assert store.search("nullable status")


def test_promote_skips_archived_knowledge(tmp_path: Path) -> None:
    """techne's soft-archive (techne#216) must stop promotion to the SHARED corpus.

    Regression: promote filtered only `superseded_by IS NULL`, so archiving an entry
    removed it from local recall but it kept re-syncing to the team repo — where it
    cannot be un-shared. Archiving is the reversible 'keep local, stop sharing' lever.
    """
    tdb = _techne_db(
        tmp_path / "techne.db",
        [
            {"type": "fact", "content": "shared and useful", "tags": "keep"},
            {
                "type": "fact",
                "content": "the clone lives at /Users/someone/working/repo",
                "tags": "machine-specific",
                "status": "archived",
            },
        ],
    )
    store = ArchivistStore(tmp_path / "arch.db")
    rep = promote_techne_knowledge(tdb, store, written_by="Prudence")

    assert rep.read == 1, "archived row must not even be read as a candidate"
    assert rep.new == 1
    assert store.search("shared and useful"), "the live entry should still promote"
    assert store.search("clone lives") == [], "archived entry must NOT reach the corpus"


def test_promote_reads_store_without_status_column(tmp_path: Path) -> None:
    """An older techne db has no `status` column; promote must not hard-fail on it."""
    tdb = _techne_db(
        tmp_path / "legacy.db",
        [{"type": "fact", "content": "legacy schema entry", "tags": "old"}],
        with_status=False,
    )
    store = ArchivistStore(tmp_path / "arch.db")
    rep = promote_techne_knowledge(tdb, store, written_by="Prudence")

    assert rep.new == 1
    assert store.search("legacy schema entry")


def test_promote_maps_and_publishes(tmp_path: Path) -> None:
    tdb = _techne_db(
        tmp_path / "techne.db",
        [
            {
                "type": "decision",
                "content": "# auth tokens\nUse JWT with 1h expiry.",
                "tags": "resource:auth-tokens,security",
            },
            {"type": "fact", "content": "CI runs on Linux only.", "tags": "ci"},
            {"type": "totally-unmapped", "content": "ignored", "tags": ""},
        ],
    )
    store = ArchivistStore(tmp_path / "arch.db")
    rep = promote_techne_knowledge(tdb, store, written_by="Prudence")

    assert rep.read == 3
    assert rep.new == 2
    assert rep.skipped == 1  # unmapped techne type

    hits = store.search("JWT")
    assert hits, "expected the auth-tokens decision to be searchable"
    e = hits[0]
    assert e.resource_name == "auth-tokens"  # resource: tag honored
    assert e.entry_type == "decision"  # type mapped
    assert e.written_by == "Prudence"
    assert "security" in e.tags and not any(t.startswith("resource:") for t in e.tags)


def test_promote_supersedes_redistilled_lesson(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "arch.db")

    v1 = _techne_db(
        tmp_path / "techne_v1.db",
        [
            {
                "type": "decision",
                "content": "Use JWT with 1h expiry.",
                "tags": "resource:auth-tokens",
            }
        ],
    )
    r1 = promote_techne_knowledge(v1, store, written_by="Prudence")
    assert r1.new == 1

    # Later: the lesson is re-distilled with new content, same resource tag.
    v2 = _techne_db(
        tmp_path / "techne_v2.db",
        [
            {
                "type": "decision",
                "content": "Use JWT with 15m expiry plus refresh tokens.",
                "tags": "resource:auth-tokens",
            }
        ],
    )
    r2 = promote_techne_knowledge(v2, store, written_by="Prudence")
    assert r2.superseded == 1, "re-distilled same-resource lesson should supersede the prior"

    # only the latest is active / findable
    assert {e.summary for e in store.search("refresh")} == {
        "Use JWT with 15m expiry plus refresh tokens."
    }
    assert store.search("1h expiry") == []


def test_promote_min_confidence_filters(tmp_path: Path) -> None:
    tdb = _techne_db(
        tmp_path / "techne.db",
        [
            {"type": "fact", "content": "low conf", "tags": "", "conf": 0.3},
            {"type": "fact", "content": "high conf", "tags": "", "conf": 0.9},
        ],
    )
    store = ArchivistStore(tmp_path / "arch.db")
    rep = promote_techne_knowledge(tdb, store, written_by="Prudence", min_confidence_numeric=0.5)
    assert rep.new == 1
    assert rep.skipped == 1
