"""Where cabal keeps an operator's state when nobody said where.

THE DEFAULT USED TO BE THE WORKING DIRECTORY, and that is how a live master key
and one operator's personal identity profile came to be committed to this repo
(#196, #206). Neither was carelessness on its own. Both were the same structural
invitation: run cabal from a clone, and the files it creates land beside the
source, where `git add` eventually finds them.

`~/.cabal/` already existed for the CLI token (`cli/client.py`). This makes it
the home for the rest, the way `techne_home()` does for techne.

NOTHING HERE CHANGES HOW A PATH ANCHORS. `data_dir` still resolves against the
directory holding the identity file, so config and data still co-locate under
one root — that design was already right. What changes is which directory that
is when the operator has not chosen one.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

#: The identity file's name, in whichever directory is in play.
IDENTITY_FILENAME = "identity.toml"

#: Overrides the default root. Named like TECHNE_HOME for the same reason: one
#: OS user may run more than one cabal, and each needs its own root.
HOME_ENV = "CABAL_HOME"


def cabal_home() -> Path:
    """Root for cabal state: ``$CABAL_HOME``, else ``~/.cabal``.

    Resolved at call time rather than at import, so a test or a second identity
    can set the env var without re-importing the module (techne learned this
    one: its ``techne_home()`` carries the same note).
    """
    env = os.environ.get(HOME_ENV)
    return Path(env).expanduser() if env else Path.home() / ".cabal"


def resolve_identity_path(explicit: str | Path | None = None, *, cwd: Path | None = None) -> Path:
    """Decide which identity file to read.

    Order, and the middle one is the whole compatibility story:

      1. **An explicit path** — ``--config``, or the container's mount. Used as
         given, even if it does not exist, because a caller who names a file
         deserves an error about that file rather than a silent fallback.
      2. **``./identity.toml``, if it exists.** Every install that predates this
         change keeps working, in place, with no migration. It logs where the
         file WOULD live now, once, so the split is visible rather than quietly
         permanent.
      3. **``<cabal_home>/identity.toml``** otherwise — the new default.

    Rule 2 is deliberately "if it exists" rather than "always". A home-based
    default that silently ignores an operator's populated working directory
    would be worse than the split it replaces: their personas would vanish and
    their database would appear empty, with nothing naming the cause.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    here = (cwd or Path.cwd()) / IDENTITY_FILENAME
    if here.exists():
        log.info(
            "cabal.identity.using_cwd",
            path=str(here),
            note=(
                f"reading {IDENTITY_FILENAME} from the working directory. New installs "
                f"default to {cabal_home() / IDENTITY_FILENAME}; move it there, or set "
                f"{HOME_ENV}, to keep operator state out of a checkout (#211)."
            ),
        )
        return here

    return cabal_home() / IDENTITY_FILENAME
