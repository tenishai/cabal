"""SessionTracker tests (NoOp + techne adapter)."""
from __future__ import annotations
import pytest
pytest.importorskip("techne")


from pathlib import Path


from cabal.core.memory.session_tracker import (
    NoOpSessionTracker,
    TechneSessionTracker,
)


def test_noop_returns_external_id_and_swallows_end() -> None:
    tracker = NoOpSessionTracker()
    sid = tracker.start(external_id="t-1", agent_identity="archivist")
    assert sid == "t-1"
    tracker.end(session_id=sid, summary="anything")  # no error


@pytest.fixture
def techne_db(tmp_path: Path) -> tuple[Path, Path]:
    import techne

    db = tmp_path / "tk.db"
    techne.init_db(db)
    return db, tmp_path


def test_techne_start_returns_session_id(techne_db) -> None:
    db_path, data_dir = techne_db
    tracker = TechneSessionTracker(db_path=db_path, data_dir=data_dir)
    sid = tracker.start(external_id="task-xyz", agent_identity="archivist")
    # techne hands the external_id back as the session_id
    assert sid == "task-xyz"


def test_techne_end_does_not_raise_on_unknown_session(techne_db) -> None:
    """A summary for an unknown session_id should be silently absorbed."""
    db_path, data_dir = techne_db
    tracker = TechneSessionTracker(db_path=db_path, data_dir=data_dir)
    # Should not raise
    tracker.end(session_id="never-started", summary="x")


def test_techne_end_with_valid_session(techne_db) -> None:
    db_path, data_dir = techne_db
    tracker = TechneSessionTracker(db_path=db_path, data_dir=data_dir)
    sid = tracker.start(external_id="real-task", agent_identity="archivist")
    tracker.end(session_id=sid, summary="Looked up a fact.")


def test_techne_failure_is_swallowed(tmp_path) -> None:
    """If the techne backend fails, tracker shouldn't propagate."""
    # Point at a directory that won't exist as a DB → techne writes will fail
    tracker = TechneSessionTracker(
        db_path=tmp_path / "missing" / "tk.db",
        data_dir=tmp_path / "missing",
    )
    # start should fall back to returning the external_id
    sid = tracker.start(external_id="t-fallback", agent_identity="x")
    assert sid == "t-fallback"
    # end should not raise
    tracker.end(session_id=sid, summary="x")
