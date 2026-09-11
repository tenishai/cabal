"""Federated-dispatch HTTP timeouts, shared by the daemon and the `cbl` CLI.

Extracted from ``cabal.core.app`` so ``cabal.cli.client`` can resolve the same
ceiling without importing the daemon (``app`` pulls FastAPI, alembic, SQLAlchemy
and structlog — none of which the CLI wants). ONE resolver, so the parse and
validation semantics of ``CABAL_DISPATCH_TIMEOUT_S`` cannot drift between the
side that waits on a peer and the side that waits on the daemon.
"""

from __future__ import annotations

import math
import os

import httpx

# Connect ceiling for federated dispatch. Deliberately NOT tied to
# CABAL_DISPATCH_TIMEOUT_S: raising the read ceiling for a slow peer must not
# also make an *unreachable* peer hang for that long before failing.
DISPATCH_CONNECT_TIMEOUT_S = 10.0

# The CLI must outlast the daemon it is waiting on. The daemon's read ceiling
# bounds only the peer hop; the CLI additionally waits for auth, bookkeeping,
# guardrails and the response trip, so a CLI pinned to the SAME number races
# the daemon and aborts an exchange the daemon would have completed. This is
# the same reasoning as DOOM_LOOP_TTL_SAFETY_FACTOR in app.py, applied to the
# other side of the hop.
CLI_DISPATCH_HEADROOM_FACTOR = 2.0


def resolve_dispatch_timeout_s() -> float:
    """Parse ``CABAL_DISPATCH_TIMEOUT_S`` (default 30, the historical value).

    Raises on an unparseable, non-positive, or non-finite value; callers MUST
    abort rather than paper over a wrong dispatch ceiling with a default.
    """
    raw = os.environ.get("CABAL_DISPATCH_TIMEOUT_S", "30")
    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(
            f"CABAL_DISPATCH_TIMEOUT_S must be a number of seconds, got {raw!r}"
        ) from None
    if seconds <= 0:
        raise ValueError(f"CABAL_DISPATCH_TIMEOUT_S must be > 0, got {seconds!r}")
    if not math.isfinite(seconds):
        # nan <= 0 is False, so a NaN or an overflow-to-inf value (e.g. '1e400')
        # slips past the check above. Every comparison against nan is False, so
        # an unchecked NaN here would silently disable both the read ceiling
        # and the connect floor.
        raise ValueError(f"CABAL_DISPATCH_TIMEOUT_S must be finite, got {seconds!r}")
    return seconds


def dispatch_timeout() -> httpx.Timeout:
    """The daemon's federated-dispatch timeout.

    Connect stays pinned at ``DISPATCH_CONNECT_TIMEOUT_S`` so a dead peer still
    fails fast no matter how high the read ceiling goes.
    """
    seconds = resolve_dispatch_timeout_s()
    return httpx.Timeout(seconds, connect=min(DISPATCH_CONNECT_TIMEOUT_S, seconds))


def cli_dispatch_timeout() -> httpx.Timeout:
    """The `cbl` client's ceiling for routes that trigger a federated dispatch.

    ONLY for those routes. `make_client()` keeps its short default for admin
    calls, so a hung daemon does not make `cbl agents list` hang for minutes —
    raising the ceiling everywhere would trade one bad failure mode for another.
    """
    seconds = resolve_dispatch_timeout_s() * CLI_DISPATCH_HEADROOM_FACTOR
    return httpx.Timeout(seconds, connect=min(DISPATCH_CONNECT_TIMEOUT_S, seconds))
