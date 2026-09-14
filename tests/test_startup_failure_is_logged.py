"""A startup failure must not be silent.

uvicorn swallows an exception raised out of `lifespan`: the process exits 3
with no output, so an abort looks identical to a clean stop. Measured in the
container three times over — a bad CABAL_DISPATCH_TIMEOUT_S, a missing
liminal package, and the #77 SQLite path. `lifespan` therefore logs
cabal.startup_failed before re-raising, and this pins that.

Driving the real lifespan_context is the point: a test of any single raise
site would pass while the guard that covers all of them was missing.
"""

from __future__ import annotations

import os

import pytest

from cabal.core.app import create_app


def _async_db_url() -> str:
    """conftest sets the sync URL alembic needs; the app wants the async driver."""
    return os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)


@pytest.mark.asyncio
async def test_a_startup_abort_is_logged_before_uvicorn_can_swallow_it(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "not-a-number")
    app = create_app(agent="all", config_path="identity.toml", db_url=_async_db_url())

    with pytest.raises(ValueError):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover — startup never completes

    out = capsys.readouterr().out
    assert "cabal.startup_failed" in out
    assert "CABAL_DISPATCH_TIMEOUT_S" in out
    assert "ValueError" in out


@pytest.mark.asyncio
async def test_the_guard_does_not_disturb_a_clean_startup(monkeypatch, capsys) -> None:
    """The wrapper must be invisible when nothing goes wrong."""
    app = create_app(agent="all", config_path="identity.toml", db_url=_async_db_url())
    async with app.router.lifespan_context(app):
        pass
    out = capsys.readouterr().out
    assert "cabal.startup_failed" not in out
    assert "cabal.ready" in out
