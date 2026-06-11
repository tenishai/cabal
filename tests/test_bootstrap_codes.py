"""Bootstrap-code mint + consume tests."""

from __future__ import annotations

from datetime import timedelta

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
        code = await issue_persona_code(s, "rook")
        await s.commit()

    async with sessionmaker() as s:
        assert await consume(s, code) == "rook"
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
    from datetime import datetime, timezone

    async with sessionmaker() as s:
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        s.add(BootstrapCode(code="PASTCD", persona_name=None, expires_at=past))
        await s.commit()

    async with sessionmaker() as s:
        with pytest.raises(BootstrapCodeError, match="expired"):
            await consume(s, "PASTCD")
