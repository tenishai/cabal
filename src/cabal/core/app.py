"""FastAPI app factory.

Wires together: config loader, db pool, auth, bulletin board, broker, routes.
Phase 1 routes: `/healthz`, `/.well-known/jwks.json`, `/.well-known/agent-card`,
`/a2a/tasks`.

Startup sequence (in `lifespan`):
    1. Resolve master key.
    2. Open async engine + sessionmaker (state.sessionmaker).
    3. Run Alembic migrations to head (skip if `--no-migrate` configured).
    4. Bootstrap signing key (ensure one active key exists).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import structlog
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from cabal import __version__
from cabal.core.auth.bootstrap import ensure_signing_key
from cabal.core.auth.master_key import resolve_master_key
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.routes import healthz, well_known

log = structlog.get_logger(__name__)


REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _run_migrations(db_url: str) -> None:
    """Run alembic upgrade head against db_url. Sync call; cheap."""
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    # Pass the URL via env (env.py reads it).
    os.environ["CABAL_DB_URL"] = db_url
    command.upgrade(cfg, "head")


def create_app(
    *,
    agent: str = "all",
    config_path: str = "identity.toml",
    db_url: str | None = None,
    allow_dev_master_key: bool = True,
    run_migrations: bool = True,
) -> FastAPI:
    """Build a FastAPI application for the given mode."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("cabal.startup", version=__version__, agent=agent, config=config_path)

        master_key = resolve_master_key(allow_dev_fallback=allow_dev_master_key)
        _app.state.master_key = master_key

        engine = make_engine(db_url)
        install_sqlite_pragmas(engine)
        sessionmaker = make_sessionmaker(engine)
        _app.state.engine = engine
        _app.state.sessionmaker = sessionmaker

        if run_migrations:
            sync_url = (
                (db_url or os.environ.get("CABAL_DB_URL", "sqlite:///data/cabal.db"))
                .replace("sqlite+aiosqlite://", "sqlite://", 1)
            )
            _run_migrations(sync_url)

        kid = await ensure_signing_key(sessionmaker, master_key=master_key)
        _app.state.active_kid = kid
        log.info("cabal.ready", active_kid=kid)

        try:
            yield
        finally:
            await engine.dispose()
            log.info("cabal.shutdown")

    app = FastAPI(
        title="Cabal Core",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
    )

    app.state.agent_mode = agent
    app.state.config_path = config_path

    app.include_router(healthz.router)
    app.include_router(well_known.router)

    return app
