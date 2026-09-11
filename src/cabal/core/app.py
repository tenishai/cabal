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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import structlog
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import select

from cabal import __version__
from cabal.agents.echo import EchoAgent
from cabal.config.identity import (
    GuardrailsConfig,
    IdentityConfig,
    load_identity,
    resolve_data_dir,
    resolve_under_data_dir,
)
from cabal.core.auth.bootstrap import ensure_signing_key
from cabal.core.auth.bootstrap_codes import issue_operator_code
from cabal.core.auth.jwks_provider import RefreshingJWKSProvider
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.auth.master_key import (
    EncryptedRowDamagedError,
    MasterKeyMismatchError,
    describe_master_key_source,
    resolve_master_key,
)
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.broker.heartbeat import HeartbeatScheduler
from cabal.core.broker.router import DispatchRouter
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import DEFAULT_DB_URL, make_engine, make_sessionmaker, open_session
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.guardrails import Guardrail
from cabal.core.guardrails_builtin import (
    DelegationBudgetGuardrail,
    DelegationEdgeGuardrail,
    DoomLoopGuardrail,
)
from cabal.core.hooks.registry import HookRegistry
from cabal.core.identity_sync import agent_name_for, sync_personas
from cabal.core.llm.budget import BudgetGuard
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.session_tracker import (
    NoOpSessionTracker,
    SessionTracker,
    TechneSessionTracker,
)
from cabal.core.memory.store import MemoryStore, NoOpMemoryStore
from cabal.core.memory.techne_store import TechneMemoryStore
from cabal.core.models import Persona
from cabal.core.observability.activity_logger import ActivityLogger
from cabal.core.observability.event_bus import EventBus
from cabal.core.observability.langfuse_client import LangfuseTraceClient
from cabal.core.observability.trace_exporter import TraceExporter
from cabal.core.routes import a2a_jsonrpc, admin, auth, healthz, tasks, well_known

log = structlog.get_logger(__name__)


def _migrations_dir() -> Path:
    """Where alembic's scripts live, resolved INSIDE the installed package (#79).

    The old resolution walked UP three parents to a repo root. That is correct
    for an editable checkout and wrong for a non-editable `pip install cabal`,
    where it lands in site-packages and finds neither `alembic.ini` nor
    `migrations/`. The escape hatch (`CABAL_ALEMBIC_MIGRATIONS`, which Docker
    sets) meant it failed loudly rather than silently, but a bare install plus
    run-server on a clean machine broke with no indication that an undocumented
    env var was the cure.

    `migrations/` now lives under `src/cabal/` and ships as package data, so
    this resolves the same way in both install shapes. `importlib.resources` is
    the API that answers "where is this package's data", rather than inferring
    it from a source file's position in a tree.
    """
    override = os.environ.get("CABAL_ALEMBIC_MIGRATIONS")
    if override:
        return Path(override)
    return Path(str(resources.files("cabal") / "migrations"))


def _ensure_sqlite_parent_dir(db_url: str) -> None:
    """Create the parent directory of a SQLite file DB before it is opened.

    SQLite creates the database file but not intermediate directories, so the
    default ``sqlite+aiosqlite:///data/cabal.db`` dies with "unable to open
    database file" on a fresh bare-metal boot. Docker masks this via the
    volume mount, so the path went unexercised. No-op for in-memory or
    non-SQLite URLs, and never blocks startup on a URL it can't parse. #77.
    """
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(db_url)
    except Exception:  # noqa: BLE001 — URL parsing must never block startup
        return
    if not parsed.get_backend_name().startswith("sqlite"):
        return
    database = parsed.database
    if not database or database == ":memory:":
        return
    parent = Path(database).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)


def _run_migrations(db_url: str) -> None:
    """Bring the schema to head (#79).

    THE CONFIG IS BUILT IN CODE, NOT READ FROM A FILE. `alembic.ini` carried
    exactly two things this path needed -- `script_location`, which is set
    below, and logging config, which is optional -- so requiring the file
    bought a second thing to package and a second way to fail on a clean
    install. It stays in the repo for the `alembic` CLI during development,
    where the CLI genuinely wants it.

    `CABAL_ALEMBIC_INI` is still honoured for a deployment that pins it.
    """
    cfg = AlembicConfig(os.environ.get("CABAL_ALEMBIC_INI") or None)
    cfg.set_main_option("script_location", str(_migrations_dir()))
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


def build_router_input_guardrails(identity: IdentityConfig | None) -> list[Guardrail]:
    """Router-level input guardrails for the production DispatchRouter (cabal#76).

    These run in ``DispatchRouter.dispatch`` BEFORE the local-vs-remote branch,
    so they are the only enforcement point that also covers ``kind="federated"``
    peers. The in-process Mason ``DoomLoopGuardrail`` (primus.py) fires at the
    subordinate boundary and never reaches a remote hop, so without this a
    delegation to a federated peer gets JWT auth + bookkeeping + AbortGraph but
    ZERO guardrails. Default-on; identity.toml ``[guardrails]`` opts out.
    """
    gconf = identity.guardrails if identity is not None else GuardrailsConfig()
    guardrails: list[Guardrail] = []
    if gconf.doom_loop:
        guardrails.append(DoomLoopGuardrail(threshold=gconf.doom_loop_threshold))
    # ORDER MATTERS ONLY FOR THE MESSAGE, not for safety: the first BLOCK
    # short-circuits, so a tight repeat is reported as a doom loop and a
    # paraphrase storm as an exhausted budget. Both stop the dispatch.
    if gconf.delegation_budget:
        guardrails.append(
            DelegationBudgetGuardrail(
                limit=gconf.delegation_budget_limit,
                caller_window_seconds=gconf.delegation_budget_caller_window_seconds,
            )
        )
    # LAST, AND THAT IS DELIBERATE THOUGH NOT LOAD-BEARING. Any BLOCK stops the
    # dispatch, so order only decides which reason the caller is told. A caller
    # storming a colleague it IS permitted to ask should hear "budget
    # exhausted"; one asking a colleague it may not ask hears the edge refusal
    # either way, because the budget guard passes it through.
    if gconf.delegation_edges:
        guardrails.append(DelegationEdgeGuardrail(edges=_delegation_edges(identity)))
    return guardrails


def _delegation_edges(identity: IdentityConfig | None) -> dict[str, frozenset[str]]:
    """Caller persona -> the AGENT names it may dispatch to (cabal#154).

    Resolution happens ONCE, here, for two reasons. Edges are declared against
    personas while `cabal_target_agent` carries an agent name, and those differ
    for a role="primus" persona; doing it at check time would mean stripping a
    suffix on the hot path, which would silently authorize a persona literally
    named "x-primus" as though it were the primus of "x". And an edge map built
    at startup from the config is one an agent cannot reach -- the property
    that makes this authorization rather than advice.

    A name that matches no declared persona passes through verbatim, so an edge
    to a federated peer is expressible.
    """
    if identity is None or not identity.personas:
        return {}
    resolved = {
        name: agent_name_for(name, persona)
        for name, persona in identity.personas.items()
    }
    return {
        caller: frozenset(resolved.get(t, t) for t in persona.may_delegate_to)
        for caller, persona in identity.personas.items()
    }


#: The operator-facing text for a key that cannot decrypt the database.
#: A MODULE CONSTANT because the README quotes it: round 2 of #92's review
#: found the code hedged ("either ... or ...") while the README still asserted
#: a single cause, which is the exact case the hedge exists for. Named here so
#: test_readme_quotes_the_current_mismatch_message can hold the two together.
MASTER_KEY_MISMATCH_MSG = (
    "Could not decrypt this database's signing key: either the master key is "
    "not the one that wrote it, or the stored row is damaged. Restore the "
    "CABAL_MASTER_KEY that wrote it, point CABAL_DB_URL at a different "
    "database, or run `cbl reset-auth` to back up the database and regenerate "
    "the signing key."
)


def create_app(
    *,
    agent: str = "all",
    config_path: str = "identity.toml",
    db_url: str | None = None,
    allow_dev_master_key: bool = False,
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
        """Startup, with the failure made VISIBLE.

        #77 already found that uvicorn exits on a startup exception without the
        traceback reaching the terminal, and wrapped ONE step (migrations) in a
        log-and-reraise. Everything after it stayed mute: a mismatched master
        key exited with status 3 and a log that simply stopped, which is most of
        why cabal#92 took DB surgery to diagnose. This wrapper covers the whole
        sequence instead of another single step.

        `started` keeps the diagnosis honest — once the body has yielded, a
        later exception is a SHUTDOWN failure and must not be reported as a
        startup one.
        """
        started = False
        try:
            async with _startup(_app):
                started = True
                yield
        except MasterKeyMismatchError:
            # `started` here for the same reason as below: nothing in the
            # shutdown path decrypts today, so this is unreachable rather than
            # wrong — but the invariant should hold structurally, not by luck
            # about what `finally` happens to call.
            if started:
                raise
            # The one startup failure with a known remedy, so it gets a real
            # message instead of a traceback (cabal#92).
            log.error(
                "cabal.master_key_mismatch",
                db_url=db_url or os.environ.get("CABAL_DB_URL") or DEFAULT_DB_URL,
                key_source=describe_master_key_source(),
                msg=MASTER_KEY_MISMATCH_MSG,
            )
            raise
        except EncryptedRowDamagedError:
            if started:
                raise
            log.error(
                "cabal.encrypted_row_damaged",
                db_url=db_url or os.environ.get("CABAL_DB_URL") or DEFAULT_DB_URL,
                msg=(
                    "The stored signing key is not well-formed ciphertext, so "
                    "no master key will decrypt it. Restore the database from a "
                    "backup, or run `cbl reset-auth` to regenerate the signing "
                    "key. Do NOT go looking for a different CABAL_MASTER_KEY."
                ),
            )
            raise
        except Exception:
            if not started:
                log.exception("cabal.startup_failed")
            else:
                # Suppressing the misleading `startup_failed` must not mean
                # saying nothing: an unlogged shutdown exception goes back to
                # uvicorn, which is the swallowing this whole wrapper exists to
                # end, just relocated to the other end of the lifespan.
                log.exception("cabal.shutdown_failed")
            raise

    @asynccontextmanager
    async def _startup(_app: FastAPI) -> AsyncIterator[None]:
        log.info("cabal.startup", version=__version__, agent=agent, config=config_path)

        master_key = resolve_master_key(allow_dev_fallback=allow_dev_master_key)
        _app.state.master_key = master_key

        # Ensure the SQLite file's directory exists before anything opens it —
        # otherwise a fresh bare-metal boot dies with "unable to open database
        # file". Resolve the same effective URL make_engine will use. #77
        _ensure_sqlite_parent_dir(db_url or os.environ.get("CABAL_DB_URL") or DEFAULT_DB_URL)

        engine = make_engine(db_url)
        install_sqlite_pragmas(engine)
        sessionmaker = make_sessionmaker(engine)
        _app.state.engine = engine
        _app.state.sessionmaker = sessionmaker

        # SecretsBackend Protocol — default Fernet+SQLite; future backends
        # (OS keychain, OpenBao, AWS/GCP SM) plug in via the same Protocol.
        from cabal.core.secrets_backend import SqliteFernetBackend

        _app.state.secrets_backend = SqliteFernetBackend(
            sessionmaker=sessionmaker, master_key=master_key
        )

        # CheckpointStore Protocol — default SQLite. Same shape: future
        # backends (Postgres, Redis, in-memory) plug in by satisfying the
        # Protocol. Wired into individual personas in persona_factory
        # only when the persona opts in via identity.toml.
        from cabal.core.checkpoint import SqliteCheckpointStore

        _app.state.checkpoint_store = SqliteCheckpointStore(sessionmaker=sessionmaker)

        if run_migrations:
            # Resolve the same effective URL as _ensure_sqlite_parent_dir /
            # make_engine (single default: DEFAULT_DB_URL), then convert the
            # async driver to sync for alembic.
            sync_url = (
                db_url or os.environ.get("CABAL_DB_URL") or DEFAULT_DB_URL
            ).replace("sqlite+aiosqlite://", "sqlite://", 1)
            # Log migration/DB-init failures loudly before re-raising. On a
            # bare-metal boot uvicorn can exit on a startup exception without
            # the traceback reaching the terminal, so the operator sees a
            # silent exit; surface it through our own (working) logger. #77
            try:
                _run_migrations(sync_url)
            except Exception:
                log.exception("cabal.migrations_failed", db_url=sync_url)
                raise

        kid = await ensure_signing_key(sessionmaker, master_key=master_key)
        _app.state.active_kid = kid

        jwks_provider = RefreshingJWKSProvider(sessionmaker=sessionmaker)
        await jwks_provider.refresh()
        _app.state.jwks_provider = jwks_provider

        await _ensure_echo_agent(sessionmaker, active_kid=kid)

        # Sync personas declared in identity.toml (if the file exists).
        identity: IdentityConfig | None = None
        config_path_obj = Path(config_path)
        _app.state.identity_path = config_path_obj
        if config_path_obj.exists():
            identity = load_identity(config_path_obj)
            async with open_session(sessionmaker) as session:
                stored = await load_active_signing_key(session, master_key=master_key)
            if stored is not None:
                await sync_personas(sessionmaker, identity, stored)
            _app.state.identity = identity
        else:
            log.info("identity.absent", path=str(config_path_obj))
            _app.state.identity = None

        # Agent data-root: where cabal-owned, provisioned resources live. Relative
        # cabal.data_dir anchors at the identity.toml dir (agent root != user home,
        # techne#441). Resolved in core so it holds with or without liminal.
        data_dir = resolve_data_dir(identity, config_path_obj)
        _app.state.data_dir = data_dir
        log.info("cabal.data_dir", path=str(data_dir))

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

        # Activity logger — always on; writes tool_call, tool_result, and
        # llm_call rows to the events table.  Not gated on Langfuse config.
        ActivityLogger(sessionmaker=sessionmaker).register(hook_registry)
        log.info("activity_logger.registered")

        event_bus = EventBus()
        _app.state.event_bus = event_bus

        http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        bookkeeper = Bookkeeper(sessionmaker, event_bus=event_bus)
        dispatch_router = DispatchRouter(
            sessionmaker=sessionmaker,
            bookkeeper=bookkeeper,
            jwks_provider=jwks_provider,
            http_client=http_client,
            hook_registry=hook_registry,
            # cabal#76 — default-on router guardrails so FEDERATED dispatch is
            # doom-loop protected (the Mason DoomLoop never reaches remote hops).
            input_guardrails=build_router_input_guardrails(identity),
        )
        dispatch_router.register_local(EchoAgent())

        # Memory store + session tracker (techne if configured, otherwise no-op).
        memory_store: MemoryStore = NoOpMemoryStore()
        session_tracker: SessionTracker = NoOpSessionTracker()
        if identity is not None and identity.techne.db_path:
            # Absolute db_path is used as-is (back-compat). NOTE: a *relative*
            # db_path MOVES — it now resolves under the agent data-root instead
            # of the old CWD-relative path, and techne re-inits an empty DB at
            # the new location. Intended (agent-root co-location), but a behavior
            # change for any deployment using a relative techne.db_path.
            techne_db = resolve_under_data_dir(identity.techne.db_path, data_dir)
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
            session_tracker = TechneSessionTracker(db_path=techne_db, data_dir=techne_db.parent)
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
            # cabal#11 — share the router's AbortGraph so a budget trip / open
            # circuit cancels the task's descendants, not just its own call.
            litellm_client.abort_coordinator = dispatch_router.abort_coordinator
            _app.state.litellm_client = litellm_client

            # If [liminal] is configured, build a shared httpx.AsyncClient
            # for outbound MCP calls and pre-fetch the active signing key
            # so persona_factory can mint per-persona JWTs.
            mcp_http_client: httpx.AsyncClient | None = None
            stored_signing_key = None
            if identity.liminal.url:
                # Fail fast at startup if liminal is missing — beats a
                # confusing error on first dispatch hours later.
                try:
                    import liminal  # noqa: F401  # availability check
                except ImportError as exc:
                    raise RuntimeError(
                        "identity.toml configures `[liminal] url = "
                        f"{identity.liminal.url!r}` but the liminal package "
                        "is not installed. Run `pip install cabal[mcp]` "
                        "(or `pip install -e ../liminal` for the "
                        "sibling-checkout dev layout)."
                    ) from exc
                mcp_http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
                _app.state.mcp_http_client = mcp_http_client
                async with open_session(sessionmaker) as session:
                    stored_signing_key = await load_active_signing_key(
                        session, master_key=master_key
                    )
                _app.state.mcp_signing_key = stored_signing_key
                log.info("liminal.wired", url=identity.liminal.url)

            from cabal.core.persona_factory import register_personas_from_identity

            registered = register_personas_from_identity(
                dispatch_router=dispatch_router,
                identity=identity,
                litellm_client=litellm_client,
                memory_store=memory_store,
                session_tracker=session_tracker,
                hook_registry=hook_registry,
                mcp_http_client=mcp_http_client,
                signing_key=stored_signing_key,
                checkpoint_store=_app.state.checkpoint_store,
            )
            registered_persona_names.extend(registered)

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
            mcp_client = getattr(_app.state, "mcp_http_client", None)
            if mcp_client is not None:
                await mcp_client.aclose()
            await engine.dispose()
            log.info("cabal.shutdown")

    # openapi_url/docs_url are only exposed when CABAL_ENABLE_DOCS=1.
    # Leaving them enabled by default leaks API surface and internal types
    # to any network-reachable client.
    _enable_docs = os.getenv("CABAL_ENABLE_DOCS") == "1"
    app = FastAPI(
        title="Cabal Core",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/openapi.json" if _enable_docs else None,
        docs_url="/docs" if _enable_docs else None,
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
