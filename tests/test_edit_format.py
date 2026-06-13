"""SEARCH/REPLACE parser + applier tests (cabal#4)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from cabal.cli.edit_format import (
    SEARCH_REPLACE_INSTRUCTIONS,
    EditBlock,
    apply_block,
    parse_search_replace_blocks,
    render_failed_match,
)

# ---- parser ----------------------------------------------------------------


def test_parse_single_block() -> None:
    text = dedent(
        """\
        Some prose.

        <<<<<<< SEARCH foo.py
        old line
        =======
        new line
        >>>>>>> REPLACE

        Trailing prose.
        """
    )
    blocks = parse_search_replace_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].path == "foo.py"
    assert blocks[0].search == "old line\n"
    assert blocks[0].replace == "new line\n"


def test_parse_multiple_blocks_preserves_order() -> None:
    text = dedent(
        """\
        <<<<<<< SEARCH a.py
        a-old
        =======
        a-new
        >>>>>>> REPLACE

        <<<<<<< SEARCH b.py
        b-old
        =======
        b-new
        >>>>>>> REPLACE
        """
    )
    blocks = parse_search_replace_blocks(text)
    assert [b.path for b in blocks] == ["a.py", "b.py"]


def test_parse_empty_search_means_create_or_append() -> None:
    text = dedent(
        """\
        <<<<<<< SEARCH new.py
        =======
        content for new file
        >>>>>>> REPLACE
        """
    )
    blocks = parse_search_replace_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].search == ""
    assert blocks[0].replace == "content for new file\n"


def test_parse_no_blocks_returns_empty() -> None:
    assert parse_search_replace_blocks("just normal prose, no markers") == []


def test_parse_skips_malformed_block_missing_replace() -> None:
    text = dedent(
        """\
        <<<<<<< SEARCH foo.py
        old
        =======
        new but no terminator
        """
    )
    assert parse_search_replace_blocks(text) == []


# ---- applier ---------------------------------------------------------------


def test_apply_block_modifies_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "foo.py"
    target.write_text("first\nsecond\nthird\n")
    block = EditBlock(path="foo.py", search="second\n", replace="SECOND\n")

    result = apply_block(block, root=tmp_path)

    assert result.applied is True
    assert result.error is None
    assert target.read_text() == "first\nSECOND\nthird\n"


def test_apply_block_creates_new_file_when_search_empty(tmp_path: Path) -> None:
    block = EditBlock(path="new.py", search="", replace="brand new\n")

    result = apply_block(block, root=tmp_path)

    assert result.applied is True
    assert (tmp_path / "new.py").read_text() == "brand new\n"


def test_apply_block_appends_to_existing_when_search_empty(tmp_path: Path) -> None:
    target = tmp_path / "log.txt"
    target.write_text("line1\n")
    block = EditBlock(path="log.txt", search="", replace="line2\n")

    result = apply_block(block, root=tmp_path)

    assert result.applied is True
    assert target.read_text() == "line1\nline2\n"


def test_apply_block_fails_when_search_not_found(tmp_path: Path) -> None:
    target = tmp_path / "foo.py"
    target.write_text("first\nsecond\n")
    block = EditBlock(path="foo.py", search="nonexistent\n", replace="ignored\n")

    result = apply_block(block, root=tmp_path)

    assert result.applied is False
    assert result.error is not None
    assert "not found" in result.error
    assert result.context == "first\nsecond\n"
    # Disk unchanged
    assert target.read_text() == "first\nsecond\n"


def test_apply_block_fails_when_search_matches_multiple_times(tmp_path: Path) -> None:
    target = tmp_path / "foo.py"
    target.write_text("dup\ndup\n")
    block = EditBlock(path="foo.py", search="dup\n", replace="ONE\n")

    result = apply_block(block, root=tmp_path)

    assert result.applied is False
    assert "matched 2 times" in (result.error or "")
    assert target.read_text() == "dup\ndup\n"


def test_apply_block_fails_when_file_missing_and_search_nonempty(
    tmp_path: Path,
) -> None:
    block = EditBlock(path="absent.py", search="x\n", replace="y\n")

    result = apply_block(block, root=tmp_path)

    assert result.applied is False
    assert "does not exist" in (result.error or "")


def test_apply_block_refuses_path_escape(tmp_path: Path) -> None:
    block = EditBlock(path="../escape.py", search="", replace="x\n")

    result = apply_block(block, root=tmp_path)

    assert result.applied is False
    assert "escapes" in (result.error or "")


# ---- failed-match rendering ------------------------------------------------


def test_render_failed_match_includes_context() -> None:
    block = EditBlock(path="foo.py", search="x\n", replace="y\n")
    from cabal.cli.edit_format import ApplyResult

    result = ApplyResult(
        block=block,
        applied=False,
        error="SEARCH block not found in 'foo.py'",
        context="actual line 1\nactual line 2\n",
    )

    out = render_failed_match(result)

    assert "SEARCH block not found" in out
    assert "actual line 1" in out
    assert "foo.py (current)" in out


def test_render_failed_match_truncates_long_context() -> None:
    from cabal.cli.edit_format import ApplyResult

    long_content = "\n".join(f"line {i}" for i in range(100))
    result = ApplyResult(
        block=EditBlock(path="big.py", search="x", replace="y"),
        applied=False,
        error="no match",
        context=long_content,
    )

    out = render_failed_match(result, max_lines=10)

    assert "more lines" in out
    assert "line 0" in out
    assert "line 99" not in out  # truncated


# ---- system prompt instructions --------------------------------------------


def test_search_replace_instructions_mention_format() -> None:
    """Sanity check — the auto-injected instructions reference the block
    markers so the LLM knows what to emit."""
    assert "SEARCH" in SEARCH_REPLACE_INSTRUCTIONS
    assert "REPLACE" in SEARCH_REPLACE_INSTRUCTIONS
    assert "=======" in SEARCH_REPLACE_INSTRUCTIONS
