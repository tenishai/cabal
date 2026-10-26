"""The guides that ship inside the wheel, read by `cbl guide`.

An agent on a machine that has `tenish-cabal` installed and no checkout of the
repo still needs to answer "what is this and what can it do for us". These
markdown files are package data, so they sit beside this module in an installed
wheel, in the Docker image (which copies only `src/`), and in an editable
checkout alike. There is one copy of each, here.

`MANIFEST` is the list of guides. A name outside it is never joined into a path,
so `cbl guide ../../etc/passwd` cannot read anything. tests/test_guides.py fails
if a manifest entry has no file, or a file here has no manifest entry.
"""

from __future__ import annotations

from importlib.resources import files

#: name -> one-line description, in the order `cbl guide` lists them.
MANIFEST: dict[str, str] = {
    "welcome": "What cabal is, where it fits, what it can do for you, and every option with why.",
}


def guide_names() -> list[str]:
    return list(MANIFEST)


def guide_text(name: str) -> str:
    """The markdown of one bundled guide. Raises KeyError for an unknown name."""
    if name not in MANIFEST:
        raise KeyError(name)
    return files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")


__all__ = ["MANIFEST", "guide_names", "guide_text"]
