"""Git blame integration — minimal, structured.

Shells out to `git blame --porcelain` and parses the output into typed
records. Async so the caller (a handler dispatch) doesn't block.

The blame is computed lazily per query — we do NOT pre-cache blame at
ingest time. Repos can be huge and most files are never blamed; the
cost only lands on files that get queried.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = ["BlameError", "BlameLine", "blame_line", "blame_summary"]


class BlameError(RuntimeError):
    """Raised when git blame fails — not a git repo, file not tracked, etc."""


@dataclass(slots=True, frozen=True)
class BlameLine:
    """One annotated line of git blame output."""

    line_number: int
    commit: str
    author: str
    author_email: str
    author_time: int  # unix timestamp
    summary: str
    content: str


async def blame_line(
    file_path: Path,
    line: int,
    *,
    repo_root: Path | None = None,
) -> BlameLine:
    """Return blame metadata for one line of one file."""
    lines = await _run_blame(file_path, line, line, repo_root=repo_root)
    if not lines:
        raise BlameError(
            f"no blame output for {file_path}:{line} — file may be empty "
            "or line out of range"
        )
    return lines[0]


async def blame_summary(
    file_path: Path,
    *,
    repo_root: Path | None = None,
) -> dict:
    """Aggregate blame stats over the whole file.

    Returns: {
        'total_lines': N,
        'authors': [{'email': ..., 'name': ..., 'lines': N, 'pct': N}, ...]
            (sorted by lines descending)
        'last_modified': latest author_time across all lines,
    }
    """
    all_lines = await _run_blame(file_path, None, None, repo_root=repo_root)
    if not all_lines:
        return {"total_lines": 0, "authors": [], "last_modified": None}
    counts: dict[tuple[str, str], int] = {}
    last_time = 0
    for line in all_lines:
        key = (line.author, line.author_email)
        counts[key] = counts.get(key, 0) + 1
        if line.author_time > last_time:
            last_time = line.author_time
    total = sum(counts.values())
    authors = [
        {
            "name": name,
            "email": email,
            "lines": n,
            "pct": round(100 * n / total, 1),
        }
        for (name, email), n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return {
        "total_lines": total,
        "authors": authors,
        "last_modified": last_time,
    }


# ---- internals ------------------------------------------------------------


async def _run_blame(
    file_path: Path,
    start: int | None,
    end: int | None,
    *,
    repo_root: Path | None,
) -> list[BlameLine]:
    git = shutil.which("git")
    if git is None:
        raise BlameError("git not found on PATH")
    if not file_path.exists():
        raise BlameError(f"{file_path} does not exist")

    cwd = repo_root or file_path.parent
    args = [git, "blame", "--porcelain"]
    if start is not None and end is not None:
        args.extend(["-L", f"{start},{end}"])
    args.append(str(file_path))

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise BlameError(
            f"git blame failed (exit {proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return _parse_porcelain(stdout.decode(errors="replace"))


def _parse_porcelain(text: str) -> list[BlameLine]:
    """Parse git blame --porcelain output.

    Each block begins with `<commit> <orig_line> <final_line> [<group_size>]`
    followed by header lines (author, author-mail, author-time, summary,
    previous, filename) and finally a content line prefixed with TAB.
    """
    out: list[BlameLine] = []
    lines = text.splitlines()
    i = 0
    # Authorship is carried by the first block for each commit; later
    # blocks for the same commit only repeat the commit hash + line numbers.
    cache: dict[str, dict] = {}
    while i < len(lines):
        first = lines[i]
        parts = first.split(" ")
        if len(parts) < 3:
            i += 1
            continue
        commit = parts[0]
        try:
            final_line = int(parts[2])
        except ValueError:
            i += 1
            continue
        meta = cache.setdefault(commit, {})
        i += 1
        while i < len(lines) and not lines[i].startswith("\t"):
            header = lines[i]
            if header.startswith("author "):
                meta["author"] = header[len("author "):]
            elif header.startswith("author-mail "):
                meta["author_email"] = header[len("author-mail "):].strip("<>")
            elif header.startswith("author-time "):
                try:
                    meta["author_time"] = int(header[len("author-time "):])
                except ValueError:
                    pass
            elif header.startswith("summary "):
                meta["summary"] = header[len("summary "):]
            i += 1
        if i < len(lines) and lines[i].startswith("\t"):
            content = lines[i][1:]
            out.append(BlameLine(
                line_number=final_line,
                commit=commit,
                author=meta.get("author", "unknown"),
                author_email=meta.get("author_email", ""),
                author_time=meta.get("author_time", 0),
                summary=meta.get("summary", ""),
                content=content,
            ))
            i += 1
    return out
