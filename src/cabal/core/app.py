"""FastAPI app factory.

Startup sequence (in `lifespan`):
    1. Resolve master key.
    2. Open async engine + sessionmaker; install pragmas.
    3. Run Alembic migrations to head.
    4. Bootstrap signing key.
    5. Refresh JWKS provider for the router.
    6. Seed a persona row for the built-in echo agent + register it in
       the bulletin board + register its LocalHandler.
    7. Mint and log a one-shot operator bootstrap code.
    8. Construct the DispatchRouter + register local handlers.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import structlog
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import select

from cabal import __version__
from cabal.agents.echo import EchoAgent
from cabal.config.identity import IdentityConfig, load_identity
from cabal.core.auth.bootstrap import ensure_signing_key
from cabal.core.auth.bootstrap_codes import issue_operator_code
from cabal.core.auth.jwks_provider import RefreshingJWKSProvider
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.auth.master_key import resolve_master_key
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.broker.heartbeat import HeartbeatScheduler
from cabal.core.broker.router import DispatchRouter
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import make_engine, make_sessionmaker, open_session
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.hooks.registry import HookRegistry
from cabal.core.identity_sync import sync_personas
from cabal.core.models import Persona
from cabal.core.routes import admin, auth, healthz, tasks, well_known

log = structlog.get_logger(__name__)


REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _run_migrations(db_url: str) -> None:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["CABAL_DB_URL"] = db_url
    command.upgrade(cfg, "head")


async def _ensure_echo_agent(sessionmaker, *, active_kid: str) -> None:
    """Idempotently seed the echo agent row in the bulletin board."""
    async with open_session(sessionmaker) as session:
        result = await session.execute(select(Persona).where(Persona.name == "echo"))
        if result.scalar_one_or_none() is None:
            session.add(Persona(name="echo", config_json={"builtin": True}))
            await session.flush()
        existing = await bulletin_store.get_active(session, "echo")
        if existing is None:
            await bulletin_store.register_agent(
                session,
                name="echo",
                persona_name="echo",
                role="builtin",
                endpoint="local://echo",
                capabilities={"skills": [{"id": "echo", "name": "Echo"}]},
                safety_tier="strict",
                agent_card_json={"name": "echo", "skills": [{"id": "echo", "name": "Echo"}]},
                agent_card_kid=active_kid,
            )


def create_app(
    *,
    agent: str = "all",
    config_path: str = "identity.toml",
    db_url: str | None = None,
    allow_dev_master_key: bool = True,
    run_migrations: bool = True,
    print_bootstrap_code: bool = True,
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

        jwks_provider = RefreshingJWKSProvider(sessionmaker=sessionmaker)
        await jwks_provider.refresh()
        _app.state.jwks_provider = jwks_provider

        await _ensure_echo_agent(sessionmaker, active_kid=kid)

        # Sync personas declared in identity.toml (if the file exists).
        identity: IdentityConfig | None = None
        config_path_obj = Path(config_path)
        if config_path_obj.exists():
            identity = load_identity(config_path_obj)
            async with open_session(sessionmaker) as session:
                stored = await load_active_signing_key(
                    session, master_key=master_key
                )
            if stored is not None:
                await sync_personas(sessionmaker, identity, stored)
            _app.state.identity = identity
        else:
            log.info("identity.absent", path=str(config_path_obj))
            _app.state.identity = None

        hook_registry = HookRegistry()
        _app.state.hook_registry = hook_registry

        http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        bookkeeper = Bookkeeper(sessionmaker)
        dispatch_router = DispatchRouter(
            sessionmaker=sessionmaker,
            bookkeeper=bookkeeper,
            jwks_provider=jwks_provider,
            http_client=http_client,
            hook_registry=hook_registry,
        )
        dispatch_router.register_local(EchoAgent())
        _app.state.dispatch_router = dispatch_router
        _app.state.http_client = http_client

        heartbeat = HeartbeatScheduler(sessionmaker)
        heartbeat.watch(["echo"])
        await heartbeat.start()
        _app.state.heartbeat = heartbeat

        if print_bootstrap_code:
            async with open_session(sessionmaker) as session:
                code = await issue_operator_code(session)
            log.warning(
                "cabal.bootstrap_code",
                code=code,
                msg=f"Operator bootstrap code (valid 5 minutes): {code}",
            )

        log.info("cabal.ready", active_kid=kid)

        try:
            yield
        finally:
            await heartbeat.stop()
            await http_client.aclose()
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
    app.include_router(auth.router)
    app.include_router(tasks.router)
    app.include_router(admin.router)

    return app
