"""Codex — code corpus persona.

MVP scope (cabal#8): file-level indexing of code repositories + git
blame integration. The corpus stores each source file as a techne
knowledge entry tagged `kind:code,language:<ext>`. The MCP handler
(cabal.handlers.code) exposes search across the corpus + a
`who_wrote(path, line?)` tool that shells to `git blame`.

Deferred to follow-ups (will require Tree-sitter):
  - AST graph: functions / classes / methods as first-class entries
    with input_schema, return type, etc.
  - Dependency graph: who_calls, what_depends_on
  - SME synthesis: blame + commit frequency + CODEOWNERS + reviewer history

What ships today is the foundation that consumer agents (Sherlock,
Reviewer) can use for "find code matching X" + "who wrote line Y" —
the two queries that drive 80%+ of code-aware agent work today.
"""

from cabal.codex.blame import BlameError, blame_line, blame_summary
from cabal.codex.repos import CodeRepo, RepoIndexer

__all__ = [
    "BlameError",
    "CodeRepo",
    "RepoIndexer",
    "blame_line",
    "blame_summary",
]
