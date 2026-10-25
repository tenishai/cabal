"""Concurrent publishes never leave two active rows for one content (#358).

Store-level, no liminal import, so these run in unit CI. Multiple PROCESSES, as
in the issue's reproduction: one process cannot race itself here.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path

from cabal.archivist import ArchivistStore, Entry


def _publish_rounds(db: str, rounds: int, supersede: bool, start) -> None:
    start.wait()
    store = ArchivistStore(Path(db))
    for i in range(rounds):
        store.publish(
            Entry(resource_name=f"r{i}", summary=f"same content {i}", entry_type="fact"),
            supersede=supersede,
        )


def _race(db: Path, *, procs: int, rounds: int, supersede: bool) -> None:
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    workers = [
        ctx.Process(target=_publish_rounds, args=(str(db), rounds, supersede, start))
        for _ in range(procs)
    ]
    for w in workers:
        w.start()
    start.set()
    for w in workers:
        w.join(60)
        assert w.exitcode == 0


def _active_per_hash(db: Path) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return [
            n
            for (n,) in conn.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE status='active' GROUP BY content_hash"
            )
        ]
    finally:
        conn.close()


def test_identical_publishes_from_many_processes_store_one_row_each(tmp_path):
    db = tmp_path / "a.db"
    ArchivistStore(db)  # create the schema before the race
    _race(db, procs=8, rounds=30, supersede=False)
    counts = _active_per_hash(db)
    assert len(counts) == 30
    assert set(counts) == {1}


def test_simultaneous_first_publishes_with_type_scope_leave_one_active(tmp_path):
    db = tmp_path / "a.db"
    ArchivistStore(db)
    _race(db, procs=8, rounds=10, supersede=True)
    conn = sqlite3.connect(db)
    try:
        per_resource = dict(
            conn.execute(
                "SELECT resource_name, COUNT(*) FROM memory_entries "
                "WHERE status='active' GROUP BY resource_name"
            ).fetchall()
        )
    finally:
        conn.close()
    assert per_resource == {f"r{i}": 1 for i in range(10)}
