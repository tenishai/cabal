"""techne -> Archivist promotion bridge (cabal Phase 1).

Builds a minimal techne `knowledge` table via raw SQL (no techne dep needed)
and promotes it into a local Archivist. Store-level only — no limenal.
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


def _techne_db(path: Path, rows: list[dict]) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            tags TEXT,
            confidence_numeric REAL NOT NULL DEFAULT 0.6,
            superseded_by INTEGER
        )"""
    )
    for r in rows:
        conn.execute(
            "INSERT INTO knowledge (type, content, tags, confidence_numeric, superseded_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["type"], r["content"], r.get("tags"), r.get("conf", 0.6), r.get("superseded_by")),
        )
    conn.commit()
    conn.close()
    return path


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
