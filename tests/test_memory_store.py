"""MemoryStore (techne adapter + NoOp) tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cabal.core.memory.store import MemoryHit, NoOpMemoryStore
from cabal.core.memory.techne_store import TechneMemoryStore


@pytest.fixture
def techne_db(tmp_path: Path) -> Path:
    import techne

    db = tmp_path / "techne.db"
    techne.init_db(db)
    return db


def test_noop_returns_empty_and_commits_nothing() -> None:
    store = NoOpMemoryStore()
    assert store.recall("anything") == []
    store.commit("fact")  # should not raise
    assert store.recall("fact") == []


def test_techne_commit_and_recall_roundtrip(techne_db) -> None:
    store = TechneMemoryStore(db_path=techne_db)
    store.commit("The sky is blue", type="fact", tags="weather", confidence="high")
    store.commit(
        "Operator decided to ship v0.6.0", type="decision", tags="release",
        confidence="medium",
    )
    hits = store.recall("sky")
    assert isinstance(hits, list)
    assert len(hits) >= 1
    assert any("sky" in h.content.lower() for h in hits)
    assert all(isinstance(h, MemoryHit) for h in hits)


def test_techne_recall_returns_no_hits_on_empty_db(techne_db) -> None:
    store = TechneMemoryStore(db_path=techne_db)
    assert store.recall("anything") == []


def test_techne_recall_limit_respected(techne_db) -> None:
    store = TechneMemoryStore(db_path=techne_db)
    for i in range(8):
        store.commit(f"sky note {i}", type="fact", tags="sky", confidence="medium")
    hits = store.recall("sky", limit=3)
    assert len(hits) <= 3


def test_techne_invalid_type_swallowed_by_commit(techne_db) -> None:
    """commit() should log + not raise on bad input."""
    store = TechneMemoryStore(db_path=techne_db)
    # techne rejects unknown types — wrapper logs and continues
    store.commit("opinion", type="totally_made_up", tags="x")
    assert store.recall("opinion") == []
