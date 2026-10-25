"""Log a caught exception without its stack frames' local variables (cabal#370).

`log.exception` hands the exception to structlog's renderer. Unconfigured, its
default ConsoleRenderer uses rich when rich is installed, and rich renders every
frame's locals (`show_locals=True`): a handler's `payload` and `ctx` (identity,
credentials), a `db_url` with its password. Neither cabal nor liminal configures
structlog, and the host process (liminal serve, the archivist's serve.py) need not
either, so library code can't rely on the renderer. This logs the plain traceback
text instead, which names files, lines and the exception, and no values.
"""

from __future__ import annotations

import traceback
from typing import Any


def log_failure(log: Any, event: str, **fields: Any) -> None:
    """`log.error(event, ...)` with the current exception's plain traceback text.

    Call from inside an `except` block, in place of `log.exception`."""
    log.error(event, **fields, traceback=traceback.format_exc())
