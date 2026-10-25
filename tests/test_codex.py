"""Tests for the Codex MVP — code corpus + git blame (cabal#8 v1)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("liminal.protocol")
pytest.importorskip("techne")

from liminal.protocol import Context  # noqa: E402

from cabal.codex import CodeRepo, RepoIndexer  # noqa: E402
from cabal.codex.blame import (  # noqa: E402
    BlameError,
    blame_line,
    blame_summary,
)
from cabal.handlers.code import CodeHandler, CodexConfig  # noqa: E402

# ---- fixtures: a tiny git repo --------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a git repo with two files + an author so blame is meaningful."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def run(*args):
        subprocess.run(args, cwd=repo, env=env, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test Author")

    (repo / "auth.py").write_text(
        "def login(user):\n"
        "    return verify_token(user.token)\n"
        "\n"
        "def verify_token(token):\n"
        "    return token == 'secret'\n"
    )
    (repo / "db.py").write_text(
        "import sqlite3\n"
        "\n"
        "def connect(path):\n"
        "    return sqlite3.connect(path)\n"
    )

    run("git", "add", ".")
    run("git", "commit", "-q", "-m", "initial commit")
    return repo


@pytest.fixture
def techne_db(tmp_path: Path) -> Path:
    from techne.core.database import init_db
    p = tmp_path / "codex.db"
    init_db(p).close()
    return p


# ---- blame ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_blame_line_returns_author(git_repo) -> None:
    result = await blame_line(
        git_repo / "auth.py", line=1, repo_root=git_repo,
    )
    assert result.author == "Test Author"
    assert result.author_email == "test@example.com"
    assert result.line_number == 1
    assert "login" in result.content


@pytest.mark.asyncio
async def test_blame_summary_aggregates_authors(git_repo) -> None:
    result = await blame_summary(
        git_repo / "auth.py", repo_root=git_repo,
    )
    assert result["total_lines"] == 5
    assert len(result["authors"]) == 1
    assert result["authors"][0]["name"] == "Test Author"
    assert result["authors"][0]["lines"] == 5
    assert result["authors"][0]["pct"] == 100.0


@pytest.mark.asyncio
async def test_blame_raises_for_missing_file(tmp_path) -> None:
    with pytest.raises(BlameError, match="does not exist"):
        await blame_line(tmp_path / "ghost.py", line=1)


@pytest.mark.asyncio
async def test_blame_raises_for_untracked_file(git_repo) -> None:
    untracked = git_repo / "fresh.py"
    untracked.write_text("x = 1\n")
    with pytest.raises(BlameError):
        await blame_line(untracked, line=1, repo_root=git_repo)


# ---- RepoIndexer ---------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_indexer_indexes_python_files(git_repo, techne_db) -> None:
    indexer = RepoIndexer(
        db_path=techne_db,
        repos=[CodeRepo(name="myrepo", root=git_repo, include_globs=["*.py"])],
    )
    result = await indexer.refresh()
    assert result.repos_indexed["myrepo"] == 2
    assert result.total == 2


@pytest.mark.asyncio
async def test_repo_indexer_is_idempotent(git_repo, techne_db) -> None:
    indexer = RepoIndexer(
        db_path=techne_db,
        repos=[CodeRepo(name="myrepo", root=git_repo, include_globs=["*.py"])],
    )
    first = await indexer.refresh()
    second = await indexer.refresh()
    assert first.total == 2
    assert second.total == 0


@pytest.mark.asyncio
async def test_repo_indexer_tags_language(git_repo, techne_db) -> None:
    """Each entry should carry language:python in tags."""
    from techne.core.database import connect

    indexer = RepoIndexer(
        db_path=techne_db,
        repos=[CodeRepo(name="myrepo", root=git_repo, include_globs=["*.py"])],
    )
    await indexer.refresh()
    conn = connect(techne_db)
    try:
        rows = conn.execute(
            "SELECT tags FROM knowledge WHERE type='document'"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        assert "language:python" in row["tags"]
        assert "kind:code" in row["tags"]
        assert "source:myrepo" in row["tags"]


# ---- CodeHandler -------------------------------------------------------


@pytest.fixture
def code_handler(git_repo, techne_db) -> CodeHandler:
    return CodeHandler(
        default_config=CodexConfig(
            db_path=techne_db,
            repos=[CodeRepo(name="myrepo", root=git_repo, include_globs=["*.py"])],
        ),
    )


def _ctx() -> Context:
    return Context(identity="codex", agent_name="codex", credentials={}, extra={})


@pytest.mark.asyncio
async def test_handler_refresh_then_search(code_handler) -> None:
    refresh_result = await code_handler.dispatch("refresh", {}, _ctx())
    assert refresh_result.is_error is False
    assert refresh_result.content[0]["data"]["total"] == 2

    search_result = await code_handler.dispatch(
        "search", {"query": "verify_token"}, _ctx(),
    )
    assert search_result.is_error is False
    hits = search_result.content[0]["data"]["hits"]
    assert any("verify_token" in h["content"] for h in hits)


@pytest.mark.asyncio
async def test_handler_search_filters_by_language(code_handler) -> None:
    await code_handler.dispatch("refresh", {}, _ctx())
    result = await code_handler.dispatch(
        "search", {"query": "login", "language": "python"}, _ctx(),
    )
    assert result.is_error is False
    hits = result.content[0]["data"]["hits"]
    for h in hits:
        assert "language:python" in h["tags"]


@pytest.mark.asyncio
async def test_handler_who_wrote_line(code_handler, git_repo) -> None:
    result = await code_handler.dispatch(
        "who_wrote",
        {"file_path": "auth.py", "repo": "myrepo", "line": 1},
        _ctx(),
    )
    assert result.is_error is False
    line_data = result.content[0]["data"]["line"]
    assert line_data["author"] == "Test Author"


@pytest.mark.asyncio
async def test_handler_who_wrote_summary(code_handler) -> None:
    result = await code_handler.dispatch(
        "who_wrote", {"file_path": "auth.py", "repo": "myrepo"}, _ctx(),
    )
    assert result.is_error is False
    summary = result.content[0]["data"]["summary"]
    assert summary["total_lines"] == 5


@pytest.mark.asyncio
async def test_handler_who_wrote_unknown_repo(code_handler) -> None:
    result = await code_handler.dispatch(
        "who_wrote", {"file_path": "x.py", "repo": "ghost"}, _ctx(),
    )
    assert result.is_error is True
    assert "unknown repo" in result.error


@pytest.mark.asyncio
async def test_handler_list_repos(code_handler) -> None:
    result = await code_handler.dispatch("list_repos", {}, _ctx())
    assert result.is_error is False
    repos = result.content[0]["data"]["repos"]
    assert len(repos) == 1
    assert repos[0]["name"] == "myrepo"


@pytest.mark.asyncio
async def test_handler_search_empty_query(code_handler) -> None:
    result = await code_handler.dispatch("search", {"query": "  "}, _ctx())
    assert result.is_error is True


@pytest.mark.asyncio
async def test_handler_unknown_tool(code_handler) -> None:
    result = await code_handler.dispatch("ghost", {}, _ctx())
    assert result.is_error is True


def test_get_handler_factory_returns_code_handler() -> None:
    from cabal.handlers.code import get_handler
    assert isinstance(get_handler(), CodeHandler)


def test_capability_card_has_required_fields() -> None:
    handler = CodeHandler()
    card = handler.capability_card()
    assert card["handler"] == "code"
    tool_names = {t["name"] for t in card["tools"]}
    assert tool_names == {"search", "who_wrote", "list_repos", "refresh"}
