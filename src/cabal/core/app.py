"""FastAPI app factory.

Wires together: config loader, db pool, bulletin board, broker, routes.
Phase 1 routes: `/healthz`, `/.well-known/jwks.json`, `/.well-known/agent-card`,
`/a2a/tasks`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI

from cabal import __version__
from cabal.core.routes import healthz, well_known

log = structlog.get_logger(__name__)


def create_app(*, agent: str = "all", config_path: str = "identity.toml") -> FastAPI:
    """Build a FastAPI application for the given mode.

    Args:
        agent: 'all' (solo) or a single agent name (scale mode).
        config_path: Path to identity.toml.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("cabal.startup", version=__version__, agent=agent, config=config_path)
        yield
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
