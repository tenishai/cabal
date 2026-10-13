"""Where the startup bootstrap code goes: `cabal.bootstrap_code` (#325)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from cabal.config.identity import IdentityConfig, resolve_bootstrap_code
from cabal.core.app import create_app


def _app():
    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    return create_app(agent="all", config_path="identity.toml", db_url=db_url)


async def _start(monkeypatch, tmp_path: Path, mode: str | None):
    monkeypatch.setenv("CABAL_HOME", str(tmp_path / "home"))
    if mode is None:
        monkeypatch.delenv("CABAL_BOOTSTRAP_CODE", raising=False)
    else:
        monkeypatch.setenv("CABAL_BOOTSTRAP_CODE", mode)
    return _app()


def _code_in_logs(logs: list[dict], code: str) -> bool:
    return any(code in repr(entry) for entry in logs)


@pytest.mark.asyncio
async def test_default_logs_the_code(monkeypatch, tmp_path) -> None:
    app = await _start(monkeypatch, tmp_path, None)
    with capture_logs() as logs:
        async with app.router.lifespan_context(app):
            pass
    [entry] = [e for e in logs if e["event"] == "cabal.bootstrap_code"]
    assert len(entry["code"]) == 6
    assert not (tmp_path / "home" / "bootstrap_code").exists()


@pytest.mark.asyncio
async def test_file_mode_keeps_the_code_out_of_the_log(monkeypatch, tmp_path) -> None:
    app = await _start(monkeypatch, tmp_path, "file")
    with capture_logs() as logs:
        async with app.router.lifespan_context(app):
            path = tmp_path / "home" / "bootstrap_code"
            code = path.read_text().strip()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post("/auth/bootstrap", json={"code": code})
            assert r.status_code == 200, r.text
    assert not _code_in_logs(logs, code)
    assert [e["path"] for e in logs if e["event"] == "cabal.bootstrap_code_written"] == [str(path)]


@pytest.mark.asyncio
async def test_file_mode_tightens_an_existing_file(monkeypatch, tmp_path) -> None:
    path = tmp_path / "home" / "bootstrap_code"
    path.parent.mkdir()
    path.write_text("OLDOLD\n")
    path.chmod(0o644)
    app = await _start(monkeypatch, tmp_path, "file")
    async with app.router.lifespan_context(app):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() != "OLDOLD"


@pytest.mark.asyncio
async def test_off_mints_nothing(monkeypatch, tmp_path) -> None:
    from sqlalchemy import func, select

    from cabal.core.db import open_session
    from cabal.core.models import BootstrapCode

    app = await _start(monkeypatch, tmp_path, "off")
    with capture_logs() as logs:
        async with app.router.lifespan_context(app):
            async with open_session(app.state.sessionmaker) as s:
                count = await s.scalar(select(func.count()).select_from(BootstrapCode))
    assert count == 0
    assert not [e for e in logs if e["event"].startswith("cabal.bootstrap_code") and e["event"] != "cabal.bootstrap_code_off"]


def test_env_wins_over_identity_and_typos_are_refused(monkeypatch) -> None:
    identity = IdentityConfig.model_validate({"cabal": {"bootstrap_code": "file"}})
    monkeypatch.delenv("CABAL_BOOTSTRAP_CODE", raising=False)
    assert resolve_bootstrap_code(identity) == "file"
    assert resolve_bootstrap_code(None) == "log"
    monkeypatch.setenv("CABAL_BOOTSTRAP_CODE", "OFF")
    assert resolve_bootstrap_code(identity) == "off"
    monkeypatch.setenv("CABAL_BOOTSTRAP_CODE", "stdout")
    with pytest.raises(ValueError, match="CABAL_BOOTSTRAP_CODE"):
        resolve_bootstrap_code(identity)


def test_identity_file_refuses_an_unknown_mode() -> None:
    with pytest.raises(ValueError):
        IdentityConfig.model_validate({"cabal": {"bootstrap_code": "stdout"}})
