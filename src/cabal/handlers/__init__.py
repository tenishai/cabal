"""Cabal-shipped MCP handlers.

These modules ship handler implementations that limenal discovers via the
`limenal.handlers` entry-point group declared in cabal's pyproject.toml.
Each handler exposes a slice of cabal's substrate (Architect's patterns
corpus, Archivist's team memory, Codex's code graph, Librarian's docs)
through limenal's standard MCP wire protocol.

Limenal stays slim — its core dep graph never grows because cabal owns
the persona-specific handler code. Operators running `pip install limenal`
without cabal get the generic built-in handlers (scratchpad, web, files,
memory, notify) only; the cabal handlers light up when cabal is also
installed.
"""
