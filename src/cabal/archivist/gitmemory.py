"""Git-backed sync for the Archivist team-memory corpus (agent-model §5b, Phase 2).

Wraps the conflict-free file representation (gitsync.export/materialize) with a git
hub: each machine holds a clone of a shared memory repo, materializes a local SQLite
view from it, promotes its own learnings in, and pushes. Because entries are
content-addressed files and supersession is append-only markers, git merges are pure
unions — no conflicts — so N people share one converging brain.

Uses the `git` CLI via subprocess; no extra deps. Limenal-free.

    ensure_clone(remote, repo_dir)                clone the memory repo if absent
    pull(repo_dir, local_db)            -> store  fetch+merge, rebuild local SQLite
    push(store, repo_dir, message=…)    -> bool   export, commit, merge remote, push
    sync_promote(techne_db, repo_dir,             the full per-agent flow:
                 local_db, written_by=…) -> dict   pull → promote → push
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cabal.archivist.gitsync import export_corpus, materialize_corpus
from cabal.archivist.store import ArchivistStore


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    )


def ensure_clone(remote: str, repo_dir: Path) -> None:
    """Clone `remote` into `repo_dir` if it isn't already a git checkout."""
    repo_dir = Path(repo_dir)
    if (repo_dir / ".git").exists():
        return
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", remote, str(repo_dir)], check=True, capture_output=True, text=True
    )
    # Default committer identity so merges never fail where global git user.name/
    # email is unset (CI, a fresh teammate machine); push() overrides per agent.
    _git(repo_dir, "config", "user.name", "cabal-archivist")
    _git(repo_dir, "config", "user.email", "archivist@local")


def pull(repo_dir: Path, local_db: Path) -> ArchivistStore:
    """Fetch + merge the memory repo, then rebuild the local SQLite view from it."""
    _git(Path(repo_dir), "pull", "--no-edit", "--no-rebase")
    return materialize_corpus(repo_dir, local_db)


def push(
    store: ArchivistStore,
    repo_dir: Path,
    *,
    message: str,
    author_name: str = "agent",
    author_email: str = "agent@local",
) -> bool:
    """Export `store` into the repo, commit any changes, merge remote, push.

    Returns True if anything was committed/pushed. Conflict-free: content-addressed
    entry files + append-only supersession markers mean the merge is a pure union.
    """
    repo_dir = Path(repo_dir)
    export_corpus(store, repo_dir)
    # Set the committer identity on the repo so BOTH the content commit and any
    # merge commit (from the pull below) have one — robust where global git
    # user.name/email is unset.
    _git(repo_dir, "config", "user.name", author_name)
    _git(repo_dir, "config", "user.email", author_email)
    _git(repo_dir, "add", "-A")
    if _git(repo_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False  # nothing new to share
    _git(repo_dir, "commit", "-m", message)
    _git(repo_dir, "pull", "--no-edit", "--no-rebase")  # absorb others' commits (union)
    _git(repo_dir, "push")
    return True


def sync_promote(
    techne_db: Path,
    repo_dir: Path,
    local_db: Path,
    *,
    written_by: str,
    author_email: str = "agent@local",
) -> dict:
    """The full bridge-to-git flow for one agent: pull team memory → promote this
    agent's distilled techne knowledge into it → push. Returns a report.

    Soft-archived knowledge (techne `status='archived'`) is NOT promoted — see
    `promote_techne_knowledge`. Archiving is therefore the reversible "keep this
    local, stop sharing it" lever, which matters here because the shared corpus
    is the one place an entry cannot be un-shared: `export_corpus` is write-only
    and `ArchivistStore` exposes no delete.
    """
    from cabal.archivist.promote import promote_techne_knowledge

    store = pull(repo_dir, local_db)
    report = promote_techne_knowledge(techne_db, store, written_by=written_by)
    pushed = push(
        store,
        repo_dir,
        message=f"{written_by}: +{report.new} new, {report.superseded} superseded",
        author_name=written_by,
        author_email=author_email,
    )
    return {"promote": report, "pushed": pushed}
