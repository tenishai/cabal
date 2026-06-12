"""SecretsBackend Protocol and the default Fernet+SQLite implementation.

The Protocol abstracts secrets storage so cabal can support multiple backends
without changing call sites. Default impl (SqliteFernetBackend) is the existing
behavior; future backends (OS keychain, GPG-encrypted file, OpenBao/Vault, AWS
Secrets Manager, GCP Secret Manager, etc.) drop in by implementing the
Protocol and registering via `[project.entry-points."cabal.secrets_backends"]`.

Wiring: `create_app` constructs the backend in lifespan and exposes it on
`app.state.secrets_backend`. Routes call `app.state.secrets_backend.<method>`
rather than threading SQLAlchemy sessions + master_key through every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.core.db import open_session
from cabal.core.secrets import (
    SecretEntry,
    get_secrets_for,
    list_secret_entries,
    purge_secrets_for,
    set_secret,
    unset_secret,
)

__all__ = ["SecretsBackend", "SqliteFernetBackend", "SecretEntry"]


@runtime_checkable
class SecretsBackend(Protocol):
    """Pluggable backend for the (persona, handler) secrets store.

    All methods are async; implementations manage their own underlying
    storage / network calls. Callers (routes, resolvers) do NOT pass
    SQLAlchemy sessions or encryption keys — those are construction-time
    concerns of the specific backend.
    """

    async def get(self, persona: str, handler: str) -> dict[str, str]:
        """Return all secrets for (persona, handler) as `{KEY: value}`.

        Empty dict if nothing is stored. Backends silently skip rows
        that fail decryption (key-rotation mismatch) so a single corrupt
        entry doesn't half-fail the whole fetch.
        """
        ...

    async def set(
        self,
        persona: str,
        handler: str,
        key: str,
        value: str,
        purpose: str | None = None,
    ) -> None:
        """Store a single secret. Overwrites if (persona, handler, key) exists."""
        ...

    async def unset(self, persona: str, handler: str, key: str) -> bool:
        """Delete a single secret. Returns True if it existed, False if no-op."""
        ...

    async def list_entries(
        self,
        persona: str | None = None,
        handler: str | None = None,
    ) -> list[SecretEntry]:
        """List secret NAMES only — never values. Optionally filtered."""
        ...

    async def purge_for(self, persona: str, handler: str) -> int:
        """Delete every secret for (persona, handler). Returns deleted count."""
        ...


@dataclass(slots=True)
class SqliteFernetBackend:
    """Default SecretsBackend: stores in cabal's SQLite DB, encrypted with Fernet.

    This is the only backend with first-party support today; it wraps the
    existing module-level functions in `cabal.core.secrets` so behavior is
    identical to pre-Protocol-extraction code.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    master_key: bytes

    async def get(self, persona: str, handler: str) -> dict[str, str]:
        async with open_session(self.sessionmaker) as session:
            return await get_secrets_for(
                session,
                master_key=self.master_key,
                persona=persona,
                handler=handler,
            )

    async def set(
        self,
        persona: str,
        handler: str,
        key: str,
        value: str,
        purpose: str | None = None,
    ) -> None:
        async with open_session(self.sessionmaker) as session:
            await set_secret(
                session,
                master_key=self.master_key,
                persona=persona,
                handler=handler,
                key=key,
                value=value,
                purpose=purpose,
            )

    async def unset(self, persona: str, handler: str, key: str) -> bool:
        async with open_session(self.sessionmaker) as session:
            return await unset_secret(
                session,
                persona=persona,
                handler=handler,
                key=key,
            )

    async def list_entries(
        self,
        persona: str | None = None,
        handler: str | None = None,
    ) -> list[SecretEntry]:
        async with open_session(self.sessionmaker) as session:
            return await list_secret_entries(
                session,
                persona=persona,
                handler=handler,
            )

    async def purge_for(self, persona: str, handler: str) -> int:
        async with open_session(self.sessionmaker) as session:
            return await purge_secrets_for(
                session,
                persona=persona,
                handler=handler,
            )
