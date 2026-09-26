"""A configured `[liminal] url` without the liminal package must stop startup.

cabal#126: the availability check sat inside the `identity.personas` guard, so
a personas-less config with a liminal URL skipped it and the daemon reported
`cabal.ready` with no tool surface. Configuring the URL is the statement of
intent; whether any persona is declared is a separate axis.
"""

from __future__ import annotations

import os
import sys

import pytest

from cabal.core.app import create_app


def _async_db_url() -> str:
    return os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)


def _hide_liminal(monkeypatch) -> None:
    # A None entry in sys.modules makes `import liminal` raise ImportError.
    monkeypatch.setitem(sys.modules, "liminal", None)


@pytest.mark.asyncio
async def test_personas_less_config_with_liminal_url_fails_fast(monkeypatch, tmp_path) -> None:
    identity = tmp_path / "identity.toml"
    identity.write_text('[liminal]\nurl = "http://127.0.0.1:9999"\n')
    _hide_liminal(monkeypatch)
    app = create_app(agent="all", config_path=str(identity), db_url=_async_db_url())

    with pytest.raises(RuntimeError, match="liminal package is not installed"):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover — startup never completes


@pytest.mark.asyncio
async def test_no_liminal_url_does_not_need_liminal(monkeypatch, tmp_path) -> None:
    """The check keys on the URL, so an unconfigured liminal stays optional."""
    identity = tmp_path / "identity.toml"
    identity.write_text("[cabal]\n")
    _hide_liminal(monkeypatch)
    app = create_app(agent="all", config_path=str(identity), db_url=_async_db_url())
    async with app.router.lifespan_context(app):
        pass
