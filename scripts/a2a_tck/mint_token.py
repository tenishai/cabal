"""Print a token the A2A compatibility kit's proxy sends to the tck persona (#261).

It is minted the way #271 admits an outside agent: one token aimed at one
persona (`aud = "tck"`, `scope = ["dispatch"]`, `sub = "agent:ext-tck"`), here
straight from cabal's database and master key, because the CI job has no
operator login. It reads $CABAL_DB_URL and $CABAL_MASTER_KEY, like cabal does.
Run it after cabal has started once, so the signing key exists.
"""

from __future__ import annotations

import asyncio

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.auth.master_key import resolve_master_key
from cabal.core.db import make_engine, make_sessionmaker, open_session


async def main() -> None:
    engine = make_engine()
    try:
        async with open_session(make_sessionmaker(engine)) as session:
            stored = await load_active_signing_key(session, master_key=resolve_master_key())
    finally:
        await engine.dispose()
    if stored is None:
        raise SystemExit("no active signing key: start cabal once first")
    print(mint(signing_key=stored, sub="agent:ext-tck", aud="tck", scope=["dispatch"], ttl_seconds=3600))


if __name__ == "__main__":
    asyncio.run(main())
