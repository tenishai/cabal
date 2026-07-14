"""In-process BrokerClient: boots a Cabal app in the same asyncio event loop.

Used by the TUI so it can stream responses without a running daemon.
The operator token is loaded from TokenStore (i.e. the file written by
`cbl login`) or passed directly for testing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from cabal.cli.client import TokenStore
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.app import create_app
from cabal.core.auth.jwt import JWTError, mint, verify
from cabal.core.auth.keys import StoredKey, load_active_signing_key
from cabal.core.broker.router import DispatchError
from cabal.core.db import open_session
from cabal.tui.client.base import BrokerError


class InProcessBrokerClient:
    """Implements BrokerClient by booting a FastAPI app in-process.

    Context manager: ``async with InProcessBrokerClient(...) as client:``.
    App is started in ``__aenter__`` and torn down in ``__aexit__``.
    """

    default_persona: str

    def __init__(
        self,
        default_persona: str = "archivist",
        config_path: str = "identity.toml",
        db_url: str | None = None,
        acompletion_fn: Any | None = None,
        operator_token: str | None = None,
    ) -> None:
        self.default_persona = default_persona
        self._config_path = config_path

        # Resolve db_url: fall back to env, then convert sync sqlite:// →
        # sqlite+aiosqlite:// (same pattern as conftest.py fixtures).
        resolved_db = db_url or os.environ.get("CABAL_DB_URL")
        if (
            resolved_db is not None
            and resolved_db.startswith("sqlite://")
            and not resolved_db.startswith("sqlite+aiosqlite://")
        ):
            resolved_db = resolved_db.replace("sqlite://", "sqlite+aiosqlite://", 1)
        self._db_url = resolved_db

        self._acompletion_fn = acompletion_fn

        # Resolve operator token: caller > TokenStore > error.
        if operator_token is None:
            operator_token = TokenStore().load()
        if operator_token is None:
            raise BrokerError(
                "no operator token found; run `cbl login` first to authenticate"
            )
        self._operator_token = operator_token

        # Per-persona in-session conversation history (A2A Messages, user/agent
        # alternating) threaded into each Task so the persona sees prior turns.
        # Lives only for the life of this process — persistence is techne's job,
        # not this. A stable contextId per persona links the turns.
        self._history: dict[str, list[Message]] = {}
        self._context_ids: dict[str, str] = {}

        # Set during __aenter__
        self._app: Any | None = None
        self._lifespan_cm: Any | None = None
        self._router: Any | None = None
        self._signing_key: StoredKey | None = None

    async def __aenter__(self) -> InProcessBrokerClient:
        self._app = create_app(
            config_path=self._config_path,
            db_url=self._db_url,
            acompletion_fn=self._acompletion_fn,
            print_bootstrap_code=False,
        )
        self._lifespan_cm = self._app.router.lifespan_context(self._app)
        await self._lifespan_cm.__aenter__()
        self._router = self._app.state.dispatch_router

        # Pre-load signing key for _mint_operator_token and dispatch-token minting.
        async with open_session(self._app.state.sessionmaker) as session:
            self._signing_key = await load_active_signing_key(
                session, master_key=self._app.state.master_key
            )

        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._lifespan_cm is not None:
            await self._lifespan_cm.__aexit__(*exc)

    async def send_message(self, persona: str, text: str) -> AsyncIterator[str]:  # type: ignore[override]
        """Dispatch ``text`` to ``persona``; yield response text deltas.

        Replicates the ``_resolve_dispatch_token`` logic from
        ``cabal.core.routes.a2a_jsonrpc``: verifies the operator token then
        mints a narrow dispatch token scoped to the target persona.

        Raises BrokerError on auth or dispatch failures before the first delta.
        """
        # Step 1: verify operator token (expected_audience=None → no aud check).
        try:
            claims = verify(
                self._operator_token,
                key_set=self._app.state.jwks_provider(),
                expected_audience=None,
            )
        except JWTError as exc:
            raise BrokerError(f"operator token invalid: {exc}") from exc

        # Step 2: resolve dispatch token (mirror _resolve_dispatch_token).
        if claims.get("aud") == persona:
            # Already a correctly-scoped dispatch token.
            dispatch_token = self._operator_token
        else:
            scope = claims.get("cabal_scope") or []
            if "operator" not in scope:
                raise BrokerError(
                    f"token aud={claims.get('aud')!r} cannot dispatch to {persona!r}; "
                    "token must have operator scope or be pre-scoped to the target"
                )
            if self._signing_key is None:
                raise BrokerError("no active signing key available after boot")
            try:
                dispatch_token = mint(
                    signing_key=self._signing_key,
                    sub=claims.get("sub", "operator"),
                    aud=persona,
                    persona=claims.get("cabal_persona"),
                    scope=["dispatch"],
                )
            except Exception as exc:  # noqa: BLE001
                raise BrokerError(f"failed to mint dispatch token: {exc}") from exc

        # Step 3: build task, threading this session's prior turns as history
        # so the persona has in-session memory. contextId links the turns.
        user_msg = Message(role="user", parts=[TextPart(text=text)])
        history = self._history.setdefault(persona, [])
        context_id = self._context_ids.setdefault(persona, str(uuid.uuid4()))
        task = Task(
            id=str(uuid.uuid4()),
            contextId=context_id,
            history=list(history),
            status=TaskStatus(state="submitted", message=user_msg),
        )

        # Step 4: stream from the router, capturing the reply so the completed
        # turn can be appended to history for subsequent messages.
        reply_chunks: list[str] = []
        try:
            async for delta in self._router.dispatch_streaming(
                target_agent=persona,
                target_generation=None,
                task=task,
                caller_jwt=dispatch_token,
            ):
                reply_chunks.append(delta)
                yield delta
        except DispatchError as exc:
            raise BrokerError(f"dispatch error: {exc}") from exc

        # Record the completed turn only if the agent actually replied. An empty
        # agent message would serialize as an empty assistant content block,
        # which some providers reject on the next turn (Prudence #101). Recording
        # the pair atomically also keeps history strictly user/agent-alternating.
        if reply_chunks:
            history.append(user_msg)
            history.append(
                Message(role="agent", parts=[TextPart(text="".join(reply_chunks))])
            )

    def _mint_operator_token(self) -> str:
        """Mint a short-lived operator token from the booted app's signing key.

        Intended for tests: call this after ``__aenter__`` to obtain a
        verifiable token, then assign it to ``self._operator_token`` before
        calling ``send_message``.

            >>> async with InProcessBrokerClient(..., operator_token="x") as c:
            ...     c._operator_token = c._mint_operator_token()
            ...     async for delta in c.send_message("archivist", "hi"): ...
        """
        if self._signing_key is None:
            raise BrokerError("_mint_operator_token called before __aenter__")
        return mint(
            signing_key=self._signing_key,
            sub="operator:test",
            aud="cabal-core",
            scope=["operator"],
        )
