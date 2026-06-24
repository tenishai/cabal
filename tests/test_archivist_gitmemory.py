"""Git-backed team-memory sync (Phase 2): two agents converge through a git hub.

Uses a local bare repo as the 'remote' — real git, no network. Store-level only
(no limenal).
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from cabal.archivist import Entry
from cabal.archivist.gitmemory import ensure_clone, pull, push


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


def _init_remote(tmp_path: Path) -> Path:
    """A bare repo seeded with one commit so clones have a `main` branch."""
    remote = tmp_path / "memory.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True
    )
    seed = tmp_path / "_seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("prudence-memory\n")
    for args in (
        ["add", "-A"],
        ["-c", "user.name=init", "-c", "user.email=i@x", "commit", "-m", "init"],
        ["push", "-u", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(seed), *args], check=True, capture_output=True)
    return remote


def test_two_agents_converge_through_git(tmp_path: Path) -> None:
    remote = _init_remote(tmp_path)

    # Agent A: learns two auth lessons (the 2nd retires the 1st), pushes.
    a_repo = tmp_path / "a_clone"
    ensure_clone(str(remote), a_repo)
    a = pull(a_repo, tmp_path / "a.db")
    a.publish(_entry("jwt 1h", resource="auth"))
    a.publish(_entry("jwt 15m + refresh", resource="auth"), supersede=True)
    assert push(a, a_repo, message="A: auth", author_name="A", author_email="a@x") is True

    # Agent B: fresh clone sees A's learnings (incl. derived supersession).
    b_repo = tmp_path / "b_clone"
    ensure_clone(str(remote), b_repo)
    b = pull(b_repo, tmp_path / "b.db")
    rows_b = _rows(tmp_path / "b.db")
    assert ("jwt 1h", "superseded") in rows_b
    assert ("jwt 15m + refresh", "active") in rows_b

    # B contributes; pushes.
    b.publish(_entry("postgres 16 in prod", resource="db"))
    assert push(b, b_repo, message="B: db", author_name="B", author_email="b@x") is True

    # A pulls → has B's, still converged on its own supersession.
    pull(a_repo, tmp_path / "a2.db")
    rows_a = _rows(tmp_path / "a2.db")
    assert ("postgres 16 in prod", "active") in rows_a
    assert ("jwt 1h", "superseded") in rows_a


def test_concurrent_divergent_pushes_merge(tmp_path: Path) -> None:
    """A and B both add without seeing each other first; the pull-before-push in
    push() merges them with no conflict (content-addressed files)."""
    remote = _init_remote(tmp_path)
    a_repo = tmp_path / "a"
    ensure_clone(str(remote), a_repo)
    a = pull(a_repo, tmp_path / "a.db")
    b_repo = tmp_path / "b"
    ensure_clone(str(remote), b_repo)
    b = pull(b_repo, tmp_path / "b.db")

    a.publish(_entry("A lesson", resource="ra"))
    b.publish(_entry("B lesson", resource="rb"))
    push(a, a_repo, message="A", author_name="A", author_email="a@x")  # first
    push(b, b_repo, message="B", author_name="B", author_email="b@x")  # pulls A, merges, pushes

    pull(a_repo, tmp_path / "final.db")
    rows = _rows(tmp_path / "final.db")
    assert ("A lesson", "active") in rows
    assert ("B lesson", "active") in rows


def test_push_noop_when_nothing_new(tmp_path: Path) -> None:
    remote = _init_remote(tmp_path)
    repo = tmp_path / "c"
    ensure_clone(str(remote), repo)
    store = pull(repo, tmp_path / "c.db")
    store.publish(_entry("once", resource="x"))
    assert push(store, repo, message="first") is True
    # re-export the same store → no new files → no commit/push
    assert push(store, repo, message="again") is False
