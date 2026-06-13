"""SEARCH/REPLACE block parsing, application, and diff rendering.

cabal#4 MVP cut — ships only the `search-replace` format (aider-validated,
simplest). `udiff` and `whole-file` formats are deferred to follow-ups:
the chat-side dispatch keys on `edit_format` so future formats slot in
beside `parse_search_replace_blocks` without touching the chat loop.

Auto-discovery is also deferred: today the operator passes
`--edit-format` on `cbl chat` and must keep it in sync with the
persona's identity.toml `edit_format`. Server-side discovery via the
AgentCard metadata is a follow-up.

Block grammar (aider-compatible):

    <<<<<<< SEARCH path/to/file.py
    existing lines, exactly as on disk
    =======
    replacement lines
    >>>>>>> REPLACE

Blocks with an empty SEARCH body insert REPLACE at the end of the file
(or create the file if it doesn't exist).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_BLOCK_RE = re.compile(
    r"<{7}\s+SEARCH\s+(?P<path>\S+)\s*\n"
    r"(?P<search>.*?)"
    r"={7}\s*\n"
    r"(?P<replace>.*?)"
    r">{7}\s+REPLACE",
    re.DOTALL,
)


@dataclass(frozen=True)
class EditBlock:
    path: str
    search: str
    replace: str


@dataclass(frozen=True)
class ApplyResult:
    block: EditBlock
    applied: bool
    error: str | None = None
    context: str | None = None  # current file content when SEARCH didn't match


def parse_search_replace_blocks(text: str) -> list[EditBlock]:
    """Extract every well-formed SEARCH/REPLACE block from `text`.

    Malformed or partial blocks are silently skipped — the caller can
    detect "no blocks parsed" by an empty list and fall back to raw
    text rendering.
    """
    blocks: list[EditBlock] = []
    for m in _BLOCK_RE.finditer(text):
        blocks.append(
            EditBlock(
                path=m.group("path").strip(),
                search=m.group("search"),
                replace=m.group("replace"),
            )
        )
    return blocks


def apply_block(block: EditBlock, *, root: Path) -> ApplyResult:
    """Apply one block to disk under `root`.

    Empty SEARCH means "append/create". Non-empty SEARCH must match
    exactly once; zero matches or multiple matches both fail (no
    auto-resolve — per task non-goals).
    """
    target = (root / block.path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return ApplyResult(
            block=block,
            applied=False,
            error=f"path {block.path!r} escapes project root",
        )

    if not block.search.strip():
        existing = target.read_text() if target.exists() else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(existing + block.replace)
        return ApplyResult(block=block, applied=True)

    if not target.exists():
        return ApplyResult(
            block=block,
            applied=False,
            error=f"file {block.path!r} does not exist",
            context=None,
        )

    current = target.read_text()
    count = current.count(block.search)
    if count == 0:
        return ApplyResult(
            block=block,
            applied=False,
            error=f"SEARCH block not found in {block.path!r}",
            context=current,
        )
    if count > 1:
        return ApplyResult(
            block=block,
            applied=False,
            error=(f"SEARCH block matched {count} times in {block.path!r}; need a unique match"),
            context=current,
        )

    target.write_text(current.replace(block.search, block.replace, 1))
    return ApplyResult(block=block, applied=True)


def render_diff(path: str, *, root: Path) -> str:
    """Return `git diff --color` for `path` relative to `root`.

    If `path` is untracked, returns the new-file diff. Returns empty
    string if there's no diff or if `git` isn't available.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--color=always", "--", path],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if result.stdout.strip():
        return result.stdout
    untracked = subprocess.run(
        ["git", "diff", "--color=always", "--no-index", "/dev/null", path],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return untracked.stdout


def render_failed_match(result: ApplyResult, *, max_lines: int = 40) -> str:
    """Format a failed-match result with file context for the operator.

    Per task non-goals: don't try to auto-resolve. Just show the actual
    file content (truncated) so the operator can see why the SEARCH
    didn't match.
    """
    lines = [f"  [skip] {result.error}"]
    if result.context is not None:
        snippet = result.context.splitlines()
        if len(snippet) > max_lines:
            shown = snippet[:max_lines]
            shown.append(f"  ... ({len(snippet) - max_lines} more lines)")
        else:
            shown = snippet
        lines.append(f"  --- {result.block.path} (current) ---")
        lines.extend(f"  {ln}" for ln in shown)
    return "\n".join(lines)


SEARCH_REPLACE_INSTRUCTIONS = """\

# Output format

When proposing code changes, emit one or more SEARCH/REPLACE blocks.
The harness parses these and applies them to disk. Format:

<<<<<<< SEARCH path/to/file.py
EXACT existing lines from the file, byte-for-byte
=======
the replacement lines
>>>>>>> REPLACE

Rules:
- The SEARCH body must match the file exactly (whitespace, indent, all).
- For a new file, use an empty SEARCH body.
- One change per block. Multiple blocks are fine; they apply in order.
- Outside of SEARCH/REPLACE blocks, you may add prose explaining the
  change — it won't be applied to disk.
"""
