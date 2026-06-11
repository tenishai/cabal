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
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import structlog
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import select

from cabal import __version__
from cabal.agents.echo import EchoAgent
from cabal.agents.personas import PERSONA_REGISTRY
from cabal.agents.personas.rook import RookPrimusPersona
from cabal.agents.subordinate import Subordinate
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
from cabal.core.llm.budget import BudgetGuard
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.session_tracker import (
    NoOpSessionTracker,
    SessionTracker,
    TechneSessionTracker,
)
from cabal.core.memory.store import MemoryStore, NoOpMemoryStore
from cabal.core.memory.techne_store import TechneMemoryStore
from cabal.core.observability.langfuse_client import LangfuseTraceClient
from cabal.core.observability.trace_exporter import TraceExporter
from cabal.core.models import Persona
from cabal.core.routes import a2a_jsonrpc, admin, auth, healthz, tasks, well_known

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


def _default_acompletion() -> Callable[..., Any]:
    """Return litellm.acompletion, importing lazily to keep tests offline."""
    import litellm  # local import — heavy module

    return litellm.acompletion


def create_app(
    *,
    agent: str = "all",
    config_path: str = "identity.toml",
    db_url: str | None = None,
    allow_dev_master_key: bool = True,
    run_migrations: bool = True,
    print_bootstrap_code: bool = True,
    acompletion_fn: Callable[..., Any] | None = None,
) -> FastAPI:
    """Build a FastAPI application for the given mode.

    `acompletion_fn` injects the LLM call-site. Default: `litellm.acompletion`
    (imported lazily). Tests pass a stub so they don't make network calls.
    """

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

        # Trace exporter (Langfuse if configured + env keys set).
        if identity is not None and identity.langfuse.host:
            trace_client = LangfuseTraceClient.from_env(
                host=identity.langfuse.host,
                public_key_env=identity.langfuse.public_key_env,
                secret_key_env=identity.langfuse.secret_key_env,
            )
            if trace_client is not None:
                TraceExporter(client=trace_client, mode="async").register(hook_registry)
                _app.state.trace_client = trace_client
                log.info("trace.langfuse_enabled", host=identity.langfuse.host)

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

        # Memory store + session tracker (techne if configured, otherwise no-op).
        memory_store: MemoryStore = NoOpMemoryStore()
        session_tracker: SessionTracker = NoOpSessionTracker()
        if identity is not None and identity.techne.db_path:
            techne_db = Path(identity.techne.db_path)
            techne_db.parent.mkdir(parents=True, exist_ok=True)
            if not techne_db.exists():
                try:
                    import techne  # lazy import

                    techne.init_db(techne_db)
                    log.info("techne.db_initialized", path=str(techne_db))
                except Exception as exc:  # noqa: BLE001
                    log.warning("techne.init_failed", error=str(exc))
            memory_store = TechneMemoryStore(
                db_path=techne_db, use_vectors=identity.techne.use_vectors
            )
            session_tracker = TechneSessionTracker(
                db_path=techne_db, data_dir=techne_db.parent
            )
            _app.state.memory_store = memory_store
            _app.state.session_tracker = session_tracker
            log.info("memory.techne_enabled", db_path=str(techne_db))

        # LLM-backed persona handlers: only personas declared in
        # identity.toml AND known to PERSONA_REGISTRY get a LocalHandler.
        # Bulletin rows for unknown personas exist (from identity_sync)
        # but dispatch returns a clear "no handler" error.
        litellm_client: LiteLLMClient | None = None
        registered_persona_names: list[str] = ["echo"]
        if identity is not None and identity.personas:
            resolved_acompletion = acompletion_fn or _default_acompletion()
            api_key_env = identity.litellm.api_key_env
            api_key = os.environ.get(api_key_env) if api_key_env else None
            budget_guard = BudgetGuard()
            _app.state.budget_guard = budget_guard
            litellm_client = LiteLLMClient(
                acompletion_fn=resolved_acompletion,
                hook_registry=hook_registry,
                api_base=identity.litellm.api_base,
                api_key=api_key,
                budget=budget_guard,
            )
            _app.state.litellm_client = litellm_client
            for persona_name, persona_cfg in identity.personas.items():
                factory = PERSONA_REGISTRY.get(persona_name)
                if factory is None:
                    log.info(
                        "personas.no_handler_registered",
                        persona=persona_name,
                        msg="declared in identity.toml but not in PERSONA_REGISTRY",
                    )
                    continue
                # Every persona gets the shared memory_store +
                # session_tracker. Both default to no-op when [techne] isn't
                # configured, so behavior is identical for non-techne deploys.
                if persona_name == "rook":
                    # Build subordinates from identity.toml.subordinates.
                    # Subordinates share the primus's memory + session tracker
                    # so reviewer/implementer recall under the same DB.
                    sub_instances: dict[str, Subordinate] = {}
                    for role, sub_cfg in persona_cfg.subordinates.items():
                        sub_instances[role] = Subordinate(
                            role=role,
                            llm_client=litellm_client,
                            model=sub_cfg.model,
                            max_tokens=sub_cfg.max_tokens,
                            memory_store=memory_store,
                            session_tracker=session_tracker,
                        )
                    if "implementer" not in sub_instances:
                        log.warning(
                            "personas.rook_missing_implementer",
                            msg="rook requires personas.rook.subordinates.implementer; skipping handler",
                        )
                        continue
                    handler = RookPrimusPersona(
                        litellm_client,
                        subordinates=sub_instances,
                        hook_registry=hook_registry,
                    )
                    # Rook primus itself also gets memory + session
                    handler.memory_store = memory_store
                    handler.session_tracker = session_tracker
                else:
                    handler = factory(
                        litellm_client,
                        memory_store=memory_store,
                        session_tracker=session_tracker,
                    )
                # Singletons keep their persona name; primus personas get
                # the -primus suffix to match identity_sync's naming.
                agent_name = (
                    f"{persona_name}-primus"
                    if persona_cfg.role == "primus"
                    else persona_name
                )
                # The handler may have its own opinion of name; force it
                # to match the agent name on the bulletin board.
                handler.name = agent_name
                dispatch_router.register_local(handler)
                registered_persona_names.append(agent_name)
                log.info(
                    "personas.handler_registered",
                    persona=persona_name,
                    agent=agent_name,
                    model=handler.model,
                )

        _app.state.dispatch_router = dispatch_router
        _app.state.http_client = http_client

        heartbeat = HeartbeatScheduler(sessionmaker)
        heartbeat.watch(registered_persona_names)
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
    app.include_router(a2a_jsonrpc.router)

    return app
