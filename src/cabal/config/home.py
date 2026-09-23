"""Where cabal keeps an operator's state when nobody said where.

THE DEFAULT USED TO BE THE WORKING DIRECTORY, and that is how a live master key
and one operator's personal identity profile came to be committed to this repo
(#196, #206). Neither was carelessness on its own. Both were the same structural
invitation: run cabal from a clone, and the files it creates land beside the
source, where `git add` eventually finds them.

`~/.cabal/` already existed for the CLI token (`cli/client.py`). This makes it
the home for the rest, the way `techne_home()` does for techne.

ONE ROOT FOR EVERY DEFAULT. The identity file, the database and the dev master
key all derive from `state_root()`, so a fresh install that names nothing cannot
split its state between the checkout and `~/.cabal` (Prudence r1 on #212 found the
first cut moved the identity file and left `cabal.db` and `.master_key` behind).
Passing only `--config` still anchors `data_dir` at that file while the database
and dev key follow `state_root()`; set `CABAL_DB_URL` too, or use `CABAL_HOME`. An
explicit value — `--config`, `CABAL_DB_URL`, `CABAL_MASTER_KEY` — still wins,
which is how the containers run, so none of this moves their state. `data_dir`
still anchors on the identity file's directory, as before.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

#: The identity file's name, in whichever directory is in play.
IDENTITY_FILENAME = "identity.toml"

#: Holds the database and the dev master key, beside the identity file.
DATA_DIRNAME = "data"

#: Files only cabal creates. Their presence marks a working directory as an
#: existing install; a bare `data/` does not, because that name is everywhere
#: (Prudence r2 on #212: a stray `data/` put the dev key in another project).
_INSTALL_MARKERS = (IDENTITY_FILENAME, f"{DATA_DIRNAME}/cabal.db", f"{DATA_DIRNAME}/.master_key")

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


_hinted: set[Path] = set()


def state_root(*, cwd: Path | None = None, announce: bool = True) -> Path:
    """The directory every default path derives from.

    The working directory, IF it already holds cabal state — ``identity.toml``,
    ``data/cabal.db`` or ``data/.master_key`` — so every install that predates
    #211 keeps working in place, with no migration. ``cabal_home()`` otherwise.

    Any one marker counts, because each alone is an install: a checkout that ran
    without an identity file still has its database in ``./data``, and ignoring
    it would start an empty cabal with nothing naming the cause.

    ``announce=False`` is for callers that only compare against the default,
    such as the master-key disagreement check, which runs in every container.
    """
    here = cwd or Path.cwd()
    if any((here / marker).exists() for marker in _INSTALL_MARKERS):
        if announce and here not in _hinted:
            _hinted.add(here)
            log.info(
                "cabal.state.using_cwd",
                path=str(here),
                note=(
                    f"using cabal state in the working directory ({IDENTITY_FILENAME}, "
                    f"{DATA_DIRNAME}/cabal.db and/or {DATA_DIRNAME}/.master_key). New installs keep it in {cabal_home()}. "
                    f"To move it: stop cabal, then move BOTH {IDENTITY_FILENAME} and "
                    f"{DATA_DIRNAME}/ into {cabal_home()} ({HOME_ENV} sets that "
                    "directory). Moving only one splits your state (#211)."
                ),
            )
        return here
    return cabal_home()


def default_db_url(*, cwd: Path | None = None, sync: bool = False) -> str:
    """SQLite URL for ``<state_root>/data/cabal.db``; ``sync`` drops aiosqlite for alembic."""
    driver = "sqlite" if sync else "sqlite+aiosqlite"
    return f"{driver}:///{state_root(cwd=cwd) / DATA_DIRNAME / 'cabal.db'}"


def default_master_key_path(*, cwd: Path | None = None, announce: bool = True) -> Path:
    """Where the dev (``--dev``) master key is read and written."""
    return state_root(cwd=cwd, announce=announce) / DATA_DIRNAME / ".master_key"


def resolve_identity_path(explicit: str | Path | None = None, *, cwd: Path | None = None) -> Path:
    """Decide which identity file to read.

    Order, and the middle one is the whole compatibility story:

      1. **An explicit path** — ``--config``, or the container's mount. Used as
         given, even if it does not exist, because a caller who names a file
         deserves an error about that file rather than a silent fallback. An
         empty string means "not given": the CLI passes ``""`` for an unset
         option, and ``Path("")`` is the working directory, not a file.
      2. **``<state_root>/identity.toml``** otherwise — the working directory
         when it already holds cabal state, else ``cabal_home()``.
    """
    if explicit:
        return Path(explicit).expanduser()
    return state_root(cwd=cwd) / IDENTITY_FILENAME
