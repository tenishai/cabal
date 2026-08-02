"""Bootstrap-code mint + consume tests."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.auth.bootstrap_codes import (
    BootstrapCodeError,
    consume,
    issue_operator_code,
    issue_persona_code,
)
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.models import Base, BootstrapCode


@pytest_asyncio.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest.mark.asyncio
async def test_operator_code_roundtrip(sessionmaker) -> None:
    async with sessionmaker() as s:
        code = await issue_operator_code(s)
        await s.commit()
        assert len(code) == 6
        assert code.isalnum() and code.isupper()

    async with sessionmaker() as s:
        persona = await consume(s, code)
        await s.commit()
        assert persona is None  # operator code


@pytest.mark.asyncio
async def test_persona_code_returns_name(sessionmaker) -> None:
    async with sessionmaker() as s:
        code = await issue_persona_code(s, "mason")
        await s.commit()

    async with sessionmaker() as s:
        assert await consume(s, code) == "mason"
        await s.commit()


@pytest.mark.asyncio
async def test_unknown_code_rejected(sessionmaker) -> None:
    async with sessionmaker() as s:
        with pytest.raises(BootstrapCodeError, match="unknown"):
            await consume(s, "NEVERPRINTED")


@pytest.mark.asyncio
async def test_double_consume_rejected(sessionmaker) -> None:
    async with sessionmaker() as s:
        code = await issue_operator_code(s)
        await s.commit()

    async with sessionmaker() as s:
        await consume(s, code)
        await s.commit()

    async with sessionmaker() as s:
        with pytest.raises(BootstrapCodeError, match="already used"):
            await consume(s, code)


@pytest.mark.asyncio
async def test_expired_code_rejected(sessionmaker) -> None:
    from datetime import datetime

    async with sessionmaker() as s:
        past = datetime.now(UTC) - timedelta(minutes=10)
        s.add(BootstrapCode(code="PASTCD", persona_name=None, expires_at=past))
        await s.commit()

    async with sessionmaker() as s:
        with pytest.raises(BootstrapCodeError, match="expired"):
            await consume(s, "PASTCD")


# ---- Route-level: misconfig must not burn the code (cabal#78) ---------------


@pytest.mark.asyncio
async def test_bootstrap_misconfig_does_not_burn_code(monkeypatch) -> None:
    """A server-side misconfig (operator_id unset) returns 500 WITHOUT
    consuming the one-shot code. Codes are for replay prevention, not
    misconfiguration punishment — the operator shouldn't lose a code (and a
    daemon restart) to a config mistake. Regression for cabal#78.

    Without the fix the operator_id check ran after the transaction committed,
    so the first call burned the code and the retry got "code already used".
    """
    import os

    from httpx import ASGITransport, AsyncClient

    from cabal.core.app import create_app
    from cabal.core.auth.bootstrap_codes import issue_operator_code
    from cabal.core.db import open_session

    # The autouse _isolate_env fixture already set CABAL_MASTER_KEY /
    # CABAL_DB_URL, created data/, and chdir'd. Override only the one thing
    # under test: remove the operator_id so the operator path 500s.
    monkeypatch.delenv("CABAL_OPERATOR_ID", raising=False)

    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    app = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    async with app.router.lifespan_context(app):
        async with open_session(app.state.sessionmaker) as s:
            code = await issue_operator_code(s)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # operator_id unconfigured → 500, but the code must survive.
            r1 = await c.post("/auth/bootstrap", json={"code": code})
            assert r1.status_code == 500, r1.text

            # Fix the config; the SAME code must still mint a token.
            monkeypatch.setenv("CABAL_OPERATOR_ID", "recovered-operator")
            r2 = await c.post("/auth/bootstrap", json={"code": code})
            assert r2.status_code == 200, r2.text
            assert r2.json()["access_token"]
