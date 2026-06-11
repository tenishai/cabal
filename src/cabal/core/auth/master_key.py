"""Master key resolution.

The master key encrypts every secret in the `signing_keys` and `secrets`
tables. In production the operator MUST set `CABAL_MASTER_KEY` (32-byte
url-safe-base64). In dev, if the env is unset and a sentinel allow file is
present, a master key is generated on disk at `data/.master_key` with 0600.

The dev fallback is loud — startup logs a warning so an operator who set
it up accidentally can never miss the regression risk.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import structlog
from cryptography.fernet import Fernet

log = structlog.get_logger(__name__)


class MasterKeyError(RuntimeError):
    """Raised when the master key is unavailable or unusable."""


def _validate_fernet_key(raw: str) -> bytes:
    """Return the bytes form of a Fernet key, raising on invalid input."""
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise MasterKeyError(
            "CABAL_MASTER_KEY is not valid base64 (use `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`)"
        ) from exc
    if len(decoded) != 32:
        raise MasterKeyError(
            f"CABAL_MASTER_KEY must decode to 32 bytes; got {len(decoded)}"
        )
    return raw.encode("ascii")


def resolve_master_key(
    *,
    env: dict[str, str] | None = None,
    dev_fallback_path: Path | None = None,
    allow_dev_fallback: bool = False,
) -> bytes:
    """Resolve a Fernet master key from env or dev fallback file.

    Args:
        env: Mapping to consult instead of os.environ (tests).
        dev_fallback_path: Where to read/write the dev master key.
        allow_dev_fallback: Must be True for the fallback to fire; otherwise
            an unset env var is a hard error.
    """
    env_map = env if env is not None else dict(os.environ)
    raw = env_map.get("CABAL_MASTER_KEY")
    if raw:
        return _validate_fernet_key(raw)

    if not allow_dev_fallback:
        raise MasterKeyError(
            "CABAL_MASTER_KEY is unset. Set it in the environment "
            "(generate with `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`) "
            "or pass --dev to enable the on-disk fallback."
        )

    path = dev_fallback_path or Path("data/.master_key")
    if path.exists():
        return _validate_fernet_key(path.read_text().strip())

    log.warning(
        "master_key.dev_fallback_generated",
        path=str(path),
        msg="Generated a dev master key on disk. DO NOT USE IN PRODUCTION.",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = Fernet.generate_key()
    path.write_bytes(generated)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return generated


def get_fernet(key: bytes) -> Fernet:
    return Fernet(key)
