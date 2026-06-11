"""Bootstrap codes: first-touch operator login.

On daemon startup we mint a single-use code (6 letter-digit chars), print
it to the log, and store it in `bootstrap_codes` with a 5-minute TTL. The
operator runs `cbl login`, types the code, gets back an operator JWT.

The flow exists so a fresh `docker compose up` can be made trustworthy
without baking secrets into env files — the code only lives in the
container's stdout, which an attacker without log access can't read.
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cabal.core.models import BootstrapCode

CODE_ALPHABET = string.ascii_uppercase + string.digits  # no lowercase, no symbols
CODE_LENGTH = 6
DEFAULT_TTL_MINUTES = 5


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _generate_code() -> str:
    """6-char code from [A-Z0-9]. Excludes look-alikes O/0/I/1 to ease typing."""
    pool = "".join(c for c in CODE_ALPHABET if c not in "O0I1")
    return "".join(secrets.choice(pool) for _ in range(CODE_LENGTH))


async def issue_operator_code(
    session: AsyncSession, *, ttl_minutes: int = DEFAULT_TTL_MINUTES
) -> str:
    """Mint a fresh operator bootstrap code (persona_name is NULL)."""
    code = _generate_code()
    expires = _now_utc() + timedelta(minutes=ttl_minutes)
    session.add(
        BootstrapCode(code=code, persona_name=None, expires_at=expires, used_at=None)
    )
    return code


async def issue_persona_code(
    session: AsyncSession, persona_name: str, *, ttl_minutes: int = DEFAULT_TTL_MINUTES
) -> str:
    code = _generate_code()
    expires = _now_utc() + timedelta(minutes=ttl_minutes)
    session.add(
        BootstrapCode(
            code=code, persona_name=persona_name, expires_at=expires, used_at=None
        )
    )
    return code


class BootstrapCodeError(RuntimeError):
    """Code unknown, expired, or already used."""


async def consume(session: AsyncSession, code: str) -> str | None:
    """Mark a code consumed if it's still valid; return its persona_name.

    Returns the persona_name from the row (None for operator codes).
    Raises BootstrapCodeError on unknown/expired/used codes.
    """
    result = await session.execute(
        select(BootstrapCode).where(BootstrapCode.code == code)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise BootstrapCodeError("unknown code")
    if row.used_at is not None:
        raise BootstrapCodeError("code already used")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _now_utc():
        raise BootstrapCodeError("code expired")
    await session.execute(
        update(BootstrapCode).where(BootstrapCode.code == code).values(used_at=_now_utc())
    )
    return row.persona_name
