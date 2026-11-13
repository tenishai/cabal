"""FastAPI app factory.

Startup sequence (in `lifespan`):
    1. Resolve master key.
    2. Open async engine + sessionmaker; install pragmas.
    3. Run Alembic migrations to head.
    4. Bootstrap signing key.
    5. Refresh JWKS provider for the router.
    6. Seed a persona row for the built-in echo agent + register it in
       the bulletin board + register its LocalHandler.
    7. Mint a one-shot operator bootstrap code and deliver it per
       `cabal.bootstrap_code` (#325): to the log (default), to an owner-only
       file, or not at all.
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
from cabal.agents.tck import TCK_ENV, TckAgent
from cabal.config.home import cabal_home, resolve_identity_path
from cabal.config.identity import (
    CabalConfig,
    GuardrailsConfig,
    IdentityConfig,
    load_identity,
    resolve_bootstrap_code,
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
from cabal.core.budget_store import (
    DelegationBudgetStore,
    SqliteDelegationBudgetStore,
)
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import (
    effective_db_url,
    make_engine,
    make_sessionmaker,
    open_session,
    sync_db_url,
)
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.detached import DetachedTasks
from cabal.core.dispatch_timeout import (
    DISPATCH_CONNECT_TIMEOUT_S,  # noqa: F401  re-exported; imported from app by tests
    dispatch_timeout,
)
from cabal.core.guardrails import Guardrail
from cabal.core.guardrails_builtin import (
    DelegationBudgetGuardrail,
    DelegationDepthGuardrail,
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
from cabal.core.observability.failures import log_failure
from cabal.core.observability.langfuse_client import LangfuseTraceClient
from cabal.core.observability.trace_exporter import TraceExporter
from cabal.core.routes import (
    a2a_jsonrpc,
    a2a_v1,
    admin,
    auth,
    directory,
    healthz,
    reviews,
    tasks,
    well_known,
)

log = structlog.get_logger(__name__)


def _migrations_dir() -> Path:
    """Where alembic's scripts live, resolved INSIDE the installed package (#79).

    The old resolution walked UP three parents to a repo root. That is correct
    for an editable checkout and wrong for a non-editable `pip install tenish-cabal`,
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

# Re-exported from cabal.core.dispatch_timeout so the `cbl` CLI can resolve the
# SAME ceiling without importing this module (which pulls FastAPI, alembic and
# SQLAlchemy). The names stay bound here because the existing tests and call
# sites import them from app; the definitions moved, the surface did not.
_dispatch_timeout = dispatch_timeout

# The doom-loop TTL must exceed the dispatch timeout by this factor. A hanging
# federated peer burns the whole read ceiling before the guard is consulted
# again, so the gap between two checks of the same delegation key is ~one
# timeout; anything less than 1.0 here defeats the guard outright, and the
# margin absorbs the rest of a hop (auth, bookkeeping, the peer's own work).
DOOM_LOOP_TTL_SAFETY_FACTOR = 2.0


def _ensure_sqlite_parent_dir(db_url: str) -> None:
    """Create the parent directory of a SQLite file DB before it is opened.

    SQLite creates the database file but not intermediate directories, so the
    default database (``<state_root>/data/cabal.db``) dies with "unable to open
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


def _hide_password(db_url: str) -> str:
    """`db_url` with any password replaced by ***, for log lines (cabal#370)."""
    try:
        from sqlalchemy.engine import make_url

        return make_url(db_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — never let redaction fail the log line
        return "<unparseable db url>"


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


async def _ensure_builtin_agent(
    sessionmaker, *, name: str, skill_name: str, description: str, active_kid: str
) -> None:
    """Idempotently seed a built-in local agent's row in the bulletin board."""
    async with open_session(sessionmaker) as session:
        result = await session.execute(select(Persona).where(Persona.name == name))
        if result.scalar_one_or_none() is None:
            session.add(Persona(name=name, config_json={"builtin": True}))
            await session.flush()
        existing = await bulletin_store.get_active(session, name)
        if existing is None:
            skills = [{"id": name, "name": skill_name}]
            card: dict[str, Any] = {"name": name, "skills": skills}
            if description:
                card["description"] = description
            await bulletin_store.register_agent(
                session,
                name=name,
                persona_name=name,
                role="builtin",
                endpoint=f"local://{name}",
                capabilities={"skills": skills},
                safety_tier="strict",
                agent_card_json=card,
                agent_card_kid=active_kid,
            )


async def _ensure_echo_agent(sessionmaker, *, active_kid: str) -> None:
    """Idempotently seed the echo agent row in the bulletin board."""
    await _ensure_builtin_agent(
        sessionmaker, name="echo", skill_name="Echo", description="", active_kid=active_kid
    )


def _tck_persona_enabled() -> bool:
    """The A2A compatibility kit's persona (#261), for the kit's CI job only."""
    return os.environ.get(TCK_ENV) == "1"


def _default_acompletion() -> Callable[..., Any]:
    """Return litellm.acompletion, importing lazily to keep tests offline."""
    import litellm  # local import — heavy module

    return litellm.acompletion


def _doom_loop_ttl(configured: float | None, dispatch_timeout_s: float) -> float | None:
    """Raise the doom-loop TTL when the dispatch timeout would defeat it.

    ``DoomLoopGuardrail`` counts N repeats of a delegation key *within* its
    TTL, resetting a key that hasn't recurred inside the window. A federated
    peer that hangs burns the entire read ceiling per attempt, so with
    ``CABAL_DISPATCH_TIMEOUT_S`` at or above the TTL every check falls outside
    the window, the count resets to 1, and the guard silently never fires --
    on the one path (``kind="federated"``) it is the sole enforcement point
    for. Measured: with the 600s default TTL the guard fires at attempt 3 for
    a 599s timeout and never for 601s.

    Returning a widened TTL rather than rejecting the config is deliberate: a
    long timeout is a legitimate deployment choice for a slow peer, and a
    guard that is present-but-wider beats one that is silently inert. ``None``
    (time-based reset disabled) is already immune and passes through.
    """
    if configured is None:
        return None
    required = dispatch_timeout_s * DOOM_LOOP_TTL_SAFETY_FACTOR
    if configured >= required:
        return configured
    log.warning(
        "guardrails.doom_loop_ttl_raised",
        configured_s=configured,
        raised_to_s=required,
        dispatch_timeout_s=dispatch_timeout_s,
        reason=(
            "TTL at or below the dispatch timeout lets a hanging federated "
            "peer reset the doom-loop count on every attempt"
        ),
    )
    return required


def build_router_input_guardrails(
    identity: IdentityConfig | None,
    *,
    dispatch_timeout_s: float = 30.0,
    budget_store: DelegationBudgetStore | None = None,
) -> list[Guardrail]:
    """Router-level input guardrails for the production DispatchRouter (cabal#76).

    These run in ``DispatchRouter.dispatch`` BEFORE the local-vs-remote branch,
    so they are the only enforcement point that also covers ``kind="federated"``
    peers. The in-process Mason ``DoomLoopGuardrail`` (primus.py) fires at the
    subordinate boundary and never reaches a remote hop, so without this a
    delegation to a federated peer gets JWT auth + bookkeeping + AbortGraph but
    ZERO guardrails. Default-on; identity.toml ``[guardrails]`` opts out.

    ``dispatch_timeout_s`` is the federated read ceiling; it is needed because
    the doom-loop TTL has to stay above it or the guard is silently defeated
    (see ``_doom_loop_ttl``). The default matches the historical httpx timeout
    so callers that don't tune dispatch keep today's behaviour -- it must stay
    in step with ``resolve_dispatch_timeout_s``'s own "30" default, which is
    the only other place that number lives.
    """
    gconf = identity.guardrails if identity is not None else GuardrailsConfig()
    guardrails: list[Guardrail] = []
    if gconf.doom_loop:
        guardrails.append(
            DoomLoopGuardrail(
                threshold=gconf.doom_loop_threshold,
                ttl_seconds=_doom_loop_ttl(gconf.doom_loop_ttl_s, dispatch_timeout_s),
            )
        )
    # ORDER MATTERS ONLY FOR THE MESSAGE, not for safety: the first BLOCK
    # short-circuits, so a tight repeat is reported as a doom loop and a
    # paraphrase storm as an exhausted budget. Both stop the dispatch.
    # cabal#144 — depth first among the three bounds. It is STRUCTURAL, so it
    # is the one a paraphrase cannot evade, and a chain already too deep should
    # hear that rather than "your budget is spent".
    if gconf.delegation_depth:
        guardrails.append(
            DelegationDepthGuardrail(max_depth=gconf.delegation_depth_limit)
        )
    if gconf.delegation_budget:
        # `budget_store=None` leaves the guard on its no-op store, which is the
        # pre-cabal#174 behaviour: bounded in memory, re-armed by a restart.
        # Every caller that has a sessionmaker should pass one; the ones that
        # do not (tests, and any embedding without a database) still get a
        # bounded guard rather than an unbounded one.
        guardrails.append(
            DelegationBudgetGuardrail(
                limit=gconf.delegation_budget_limit,
                caller_window_seconds=gconf.delegation_budget_caller_window_seconds,
                store=budget_store,
                retention_s=gconf.delegation_budget_retention_s,
            )
        )
    # LAST, AND THAT IS DELIBERATE THOUGH NOT LOAD-BEARING. Any BLOCK stops the
    # dispatch, so order only decides which reason the caller is told. A caller
    # storming a colleague it IS permitted to ask should hear "budget
    # exhausted"; one asking a colleague it may not ask hears the edge refusal
    # either way, because the budget guard passes it through.
    if gconf.delegation_edges:
        guardrails.append(
            DelegationEdgeGuardrail(
                edges=_delegation_edges(identity),
                caller_edges=_caller_edges(identity),
            )
        )
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


def _caller_edges(identity: IdentityConfig | None) -> dict[str, frozenset[str]]:
    """``agent:<name>`` sub -> the AGENT names it may dispatch to (cabal#189).

    Targets resolve exactly as `_delegation_edges` resolves them; the keys are
    subs and stay subs, prefix included, so they never meet a persona name.
    """
    if identity is None:
        return {}
    resolved = {
        name: agent_name_for(name, persona)
        for name, persona in identity.personas.items()
    }
    return {
        sub: frozenset(resolved.get(t, t) for t in targets)
        for sub, targets in identity.guardrails.caller_edges.items()
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


#: The file `bootstrap_code = "file"` writes, under cabal_home() (#325).
BOOTSTRAP_CODE_FILENAME = "bootstrap_code"


async def _issue_bootstrap_code(sessionmaker: Any, mode: str) -> None:
    """Mint the startup operator code and deliver it the way `mode` says.

    "log" is the original channel: `make bootstrap` and the k8s steps grep the
    code out of the daemon log. Logs get copied and shipped, though, so "file"
    keeps the code out of them (#325): it goes to an owner-only file and the log
    names only the path. "off" mints nothing, for a cabal that already has its
    operator.
    """
    if mode == "off":
        log.info("cabal.bootstrap_code_off")
        return
    async with open_session(sessionmaker) as session:
        code = await issue_operator_code(session)
    if mode == "file":
        path = cabal_home() / BOOTSTRAP_CODE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # O_CREAT's mode applies only to a new file. Tighten an old one before
        # the code is written, so it is never readable under the old mode.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"{code}\n")
        log.warning(
            "cabal.bootstrap_code_written",
            path=str(path),
            msg=f"Operator bootstrap code (valid 5 minutes) written to {path}",
        )
        return
    log.warning(
        "cabal.bootstrap_code",
        code=code,
        msg=f"Operator bootstrap code (valid 5 minutes): {code}",
    )


def create_app(
    *,
    agent: str = "all",
    config_path: str | None = None,
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
                db_url=_hide_password(effective_db_url(db_url)),
                key_source=describe_master_key_source(),
                msg=MASTER_KEY_MISMATCH_MSG,
            )
            raise
        except EncryptedRowDamagedError:
            if started:
                raise
            log.error(
                "cabal.encrypted_row_damaged",
                db_url=_hide_password(effective_db_url(db_url)),
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
                log_failure(log, "cabal.startup_failed")
            else:
                # Suppressing the misleading `startup_failed` must not mean
                # saying nothing: an unlogged shutdown exception goes back to
                # uvicorn, which is the swallowing this whole wrapper exists to
                # end, just relocated to the other end of the lifespan.
                log_failure(log, "cabal.shutdown_failed")
            raise

    @asynccontextmanager
    async def _startup(_app: FastAPI) -> AsyncIterator[None]:
        log.info("cabal.startup", version=__version__, agent=agent, config=config_path)

        master_key = resolve_master_key(allow_dev_fallback=allow_dev_master_key)
        _app.state.master_key = master_key

        # Ensure the SQLite file's directory exists before anything opens it —
        # otherwise a fresh bare-metal boot dies with "unable to open database
        # file". Resolve the same effective URL make_engine will use. #77
        _ensure_sqlite_parent_dir(effective_db_url(db_url))

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
            # make_engine (single default: effective_db_url), then convert the
            # async driver to sync for alembic.
            sync_url = sync_db_url(db_url)
            # Log migration/DB-init failures loudly before re-raising. On a
            # bare-metal boot uvicorn can exit on a startup exception without
            # the traceback reaching the terminal, so the operator sees a
            # silent exit; surface it through our own (working) logger. #77
            try:
                _run_migrations(sync_url)
            except Exception:
                log_failure(log, "cabal.migrations_failed", db_url=_hide_password(sync_url))
                raise

        kid = await ensure_signing_key(sessionmaker, master_key=master_key)
        _app.state.active_kid = kid

        jwks_provider = RefreshingJWKSProvider(sessionmaker=sessionmaker)
        await jwks_provider.refresh()
        _app.state.jwks_provider = jwks_provider

        await _ensure_echo_agent(sessionmaker, active_kid=kid)
        if _tck_persona_enabled():
            await _ensure_builtin_agent(
                sessionmaker,
                name="tck",
                skill_name="A2A TCK scenarios",
                description="Acts out the A2A compatibility kit's scenarios (test only).",
                active_kid=kid,
            )

        # Sync personas declared in identity.toml (if the file exists).
        identity: IdentityConfig | None = None
        # None means "nobody chose" — resolve_identity_path decides between the
        # working directory and ~/.cabal (#211). An explicit value, including the
        # container's /app/identity.toml, is used exactly as given.
        config_path_obj = resolve_identity_path(config_path)
        _app.state.identity_path = config_path_obj
        _app.state.config_path = str(config_path_obj)
        log.info(
            "cabal.identity_path",
            path=str(config_path_obj),
            db_url=_hide_password(effective_db_url(db_url)),
        )
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
        # Resolved here, before the heartbeat and detached-task machinery
        # start, so a typo in $CABAL_BOOTSTRAP_CODE fails the start without
        # leaving those running (#325).
        bootstrap_mode = resolve_bootstrap_code(identity)
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

        # Federated dispatch rides this client; see _dispatch_timeout for why
        # the read ceiling is tunable and the connect ceiling is not.
        resolved_dispatch_timeout = _dispatch_timeout()
        http_client = httpx.AsyncClient(timeout=resolved_dispatch_timeout)
        bookkeeper = Bookkeeper(sessionmaker, event_bus=event_bus)
        # cabal#174 — the delegation budget must survive a restart, because the
        # incident it exists for ended in one: liveness-killed, restarted,
        # resumed from persisted task state against a counter that did not
        # persist, and the storm was rebuilt against a fresh budget.
        #
        # HYDRATED HERE, BEFORE THE ROUTER IS SERVING, and that is the only
        # read the guard ever does: `check()` must stay await-free (a primus
        # fan-out shares one instance across concurrent legs), so it cannot
        # touch an aiosqlite store. Writes are behind.
        router_guardrails = build_router_input_guardrails(
            identity,
            dispatch_timeout_s=resolved_dispatch_timeout.read,
            budget_store=SqliteDelegationBudgetStore(sessionmaker=sessionmaker),
        )
        for _g in router_guardrails:
            _hydrate = getattr(_g, "hydrate", None)
            if _hydrate is not None:
                # hydrate() swallows its own store errors and returns 0, so a
                # cold or broken table degrades to the in-memory bound rather
                # than taking startup with it.
                #
                # THE #77 CITATION THIS COMMENT USED TO CARRY WAS WRONG, and the
                # pre-pass caught it. It claimed #77's lesson is "a startup that
                # exits on a recoverable condition is worse than a weaker bound".
                # #77 argues close to the opposite emphasis: its two fixes were
                # to CREATE the missing directory (eliminate the recoverable
                # condition rather than tolerate it) and to SURFACE swallowed
                # startup errors, and its "Why it matters" is about visibility --
                # "it cost three diagnostic rounds to see errors that should have
                # been on the terminal".
                #
                # What #77 does support is the half that is easy to get wrong
                # here: degrading QUIETLY would be the defect. So hydrate logs at
                # exception level, and the reason to degrade at all is argued on
                # its own merits -- a budget guard that refuses to boot leaves
                # the fleet with NO bound, which is strictly worse than the
                # in-memory bound it had before cabal#174.
                log.info("cabal.delegation_budget.hydrated", keys=await _hydrate())

        # Subject-wide token revocations (2026-09-24): loaded once the
        # migrations have run, shared by every verification site.
        from cabal.core.auth.revocations import RevocationList

        revocations = await RevocationList.load(sessionmaker)
        _app.state.revocations = revocations

        dispatch_router = DispatchRouter(
            sessionmaker=sessionmaker,
            bookkeeper=bookkeeper,
            jwks_provider=jwks_provider,
            revocations=revocations,
            http_client=http_client,
            hook_registry=hook_registry,
            # cabal#76 — default-on router guardrails so FEDERATED dispatch is
            # doom-loop protected (the Mason DoomLoop never reaches remote hops).
            # The read ceiling is passed through so the doom-loop TTL can be
            # kept above it -- see _doom_loop_ttl.
            input_guardrails=router_guardrails,
            max_wait_continuations=(
                identity.cabal if identity is not None else CabalConfig()
            ).max_wait_continuations,
        )
        dispatch_router.register_local(EchoAgent())
        if _tck_persona_enabled():
            dispatch_router.register_local(TckAgent())

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

        # Fail fast at startup if [liminal] url is set but liminal is missing —
        # beats a confusing error on first dispatch hours later. Driven by the
        # URL alone, NOT nested under the personas guard below (cabal#126): a
        # personas-less config with a liminal URL used to skip this check and
        # come up "ready" with no tool surface. Setting the URL is the intent.
        if identity is not None and identity.liminal.url:
            try:
                import liminal  # noqa: F401  # availability check
            except ImportError as exc:
                raise RuntimeError(
                    "identity.toml configures `[liminal] url = "
                    f"{identity.liminal.url!r}` but the liminal package "
                    "is not installed. Run `pip install tenish-cabal[mcp]` "
                    "(or `pip install -e ../liminal` for the "
                    "sibling-checkout dev layout)."
                ) from exc

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

        # Detached dispatches (#277): nothing runs yet in this process, so a
        # task still submitted or working was cut off by a stop or a crash.
        await bookkeeper.fail_interrupted()
        cabal_cfg = identity.cabal if identity is not None else CabalConfig()
        _app.state.detached = DetachedTasks(
            limit=cabal_cfg.detached_task_limit,
            abort=dispatch_router.abort_coordinator,
            running=dispatch_router.running,
        )

        # Scheduled dispatches (issue 406): rows in `scheduled`, started when due.
        from cabal.core.scheduler import Scheduler

        scheduler = Scheduler(app_state=_app.state, interval_seconds=cabal_cfg.schedule_poll_seconds)
        _app.state.scheduler = scheduler
        await scheduler.start()

        heartbeat = HeartbeatScheduler(sessionmaker)
        heartbeat.watch(registered_persona_names)
        await heartbeat.start()
        _app.state.heartbeat = heartbeat

        if print_bootstrap_code:
            await _issue_bootstrap_code(sessionmaker, bootstrap_mode)

        log.info("cabal.ready", active_kid=kid)

        try:
            yield
        finally:
            await scheduler.stop()  # before the drain, so nothing new starts under it
            await _app.state.detached.drain(cabal_cfg.detached_drain_seconds)
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
    app.include_router(reviews.router)
    app.include_router(directory.router)
    app.include_router(a2a_jsonrpc.router)
    app.include_router(a2a_v1.router)

    return app
