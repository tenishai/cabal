"""DB-backed encrypted secret store.

Companion to the `CABAL_SECRET_<persona>_<handler>_<KEY>` env-var path
(Phase GG). The DB store is the durable, hot-updatable surface:

  - Encrypted at rest with Fernet (the same master_key that protects
    signing keys).
  - Keyed by `<persona>:<handler>:<KEY>` so multi-underscore handler /
    key names don't collide with the env-var prefix split.
  - Hot updates: `cbl secret set` writes to the table; the next call
    to the `/admin/secrets/{persona}/{handler}` endpoint sees the new
    value with no daemon restart.

Resolution order in the secrets endpoint:
  1. DB-backed values for (persona, handler) — lowest priority
  2. Env vars matching CABAL_SECRET_<persona>_<handler>_<KEY> — override

Operators get a quick-override path (drop a value into the shell for
testing) without having to wipe the DB state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cabal.core.models import Secret

NAME_SEP = ":"


def make_name(persona: str, handler: str, key: str) -> str:
    """Canonical secret name: `<persona>:<handler>:<KEY>`."""
    return f"{persona}{NAME_SEP}{handler}{NAME_SEP}{key}"


def _name_starts_with(prefix: str) -> ColumnElement[bool]:
    """Exact, case-sensitive prefix test on `Secret.name`. Not LIKE: LIKE
    treats `_` and `%` as wildcards and SQLite's LIKE ignores ASCII case, so a
    persona such as `_lice` or `ALICE` reached alice's secrets (cabal#252)."""
    return func.substr(Secret.name, 1, len(prefix)) == prefix


def parse_name(name: str) -> tuple[str, str, str]:
    """Inverse of make_name. Raises ValueError on malformed input."""
    parts = name.split(NAME_SEP, 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"secret name {name!r} not in <persona>:<handler>:<KEY> form"
        )
    return parts[0], parts[1], parts[2]


@dataclass(slots=True, frozen=True)
class SecretEntry:
    persona: str
    handler: str
    key: str

    @property
    def name(self) -> str:
        return make_name(self.persona, self.handler, self.key)


async def set_secret(
    session: AsyncSession,
    *,
    master_key: bytes,
    persona: str,
    handler: str,
    key: str,
    value: str,
    purpose: str | None = None,
) -> None:
    """Encrypt and persist (or update) a secret.

    The (persona, handler, key) tuple is unique — second call overwrites
    rather than failing, matching what an operator typing the same
    command twice expects.
    """
    fernet = Fernet(master_key)
    encrypted = fernet.encrypt(value.encode("utf-8"))
    name = make_name(persona, handler, key)
    existing = await session.get(Secret, name)
    if existing is not None:
        existing.value_encrypted = encrypted
        if purpose is not None:
            existing.purpose = purpose
    else:
        session.add(
            Secret(name=name, value_encrypted=encrypted, purpose=purpose)
        )


async def unset_secret(
    session: AsyncSession,
    *,
    persona: str,
    handler: str,
    key: str,
) -> bool:
    """Delete a secret. Returns True if a row existed, False if no-op."""
    name = make_name(persona, handler, key)
    existing = await session.get(Secret, name)
    if existing is None:
        return False
    await session.delete(existing)
    # Flush so a follow-up unset_secret in the same session sees the
    # deletion and returns False, not True from the stale identity map.
    await session.flush()
    return True


async def get_secrets_for(
    session: AsyncSession,
    *,
    master_key: bytes,
    persona: str,
    handler: str,
) -> dict[str, str]:
    """Decrypt every secret matching (persona, handler) and return as
    {KEY: value}. Decryption failures are silently skipped — they
    indicate a key rotation mismatch and the secrets endpoint shouldn't
    half-fail because of one corrupt row."""
    fernet = Fernet(master_key)
    prefix = f"{persona}{NAME_SEP}{handler}{NAME_SEP}"
    result = await session.execute(
        select(Secret).where(_name_starts_with(prefix))
    )
    out: dict[str, str] = {}
    for row in result.scalars():
        try:
            value = fernet.decrypt(row.value_encrypted).decode("utf-8")
        except (InvalidToken, ValueError):
            continue
        # Strip the prefix to recover just the KEY component.
        _, _, key = parse_name(row.name)
        out[key] = value
    return out


async def list_secret_entries(
    session: AsyncSession,
    *,
    persona: str | None = None,
    handler: str | None = None,
) -> list[SecretEntry]:
    """Return the names only — never the values — so the listing
    endpoint can show the operator what's stored without exposing
    secrets."""
    stmt = select(Secret.name)
    if persona is not None and handler is not None:
        stmt = stmt.where(
            _name_starts_with(f"{persona}{NAME_SEP}{handler}{NAME_SEP}")
        )
    elif persona is not None:
        stmt = stmt.where(_name_starts_with(f"{persona}{NAME_SEP}"))
    stmt = stmt.order_by(Secret.name)
    result = await session.execute(stmt)
    entries: list[SecretEntry] = []
    for (name,) in result.all():
        try:
            p, h, k = parse_name(name)
        except ValueError:
            continue
        # A handler-only filter compares the parsed name: a SQL substring test
        # would need the same wildcard and case care as the prefix (cabal#252).
        if handler is not None and h != handler:
            continue
        entries.append(SecretEntry(persona=p, handler=h, key=k))
    return entries


async def purge_secrets_for(
    session: AsyncSession,
    *,
    persona: str,
    handler: str,
) -> int:
    """Delete every secret for (persona, handler). Returns deleted count."""
    prefix = f"{persona}{NAME_SEP}{handler}{NAME_SEP}"
    result = await session.execute(
        delete(Secret).where(_name_starts_with(prefix))
    )
    return result.rowcount or 0


def merge_with_env(
    *,
    db_values: dict[str, str],
    env_values: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Combine DB-stored secrets with env-var overrides. Env wins so an
    operator can paste `CABAL_SECRET_...=x` into a shell for one-off
    testing without disturbing persisted state."""
    merged = dict(db_values)
    for key, value in env_values:
        merged[key] = value
    return merged
