"""Master key resolution.

The master key encrypts every secret in the `signing_keys` and `secrets`
tables. In production the operator MUST set `CABAL_MASTER_KEY` (32-byte
url-safe-base64). In dev, if the env is unset and a sentinel allow file is
present, a master key is generated on disk at `<state_root>/data/.master_key`
(see cabal.config.home) with 0600.

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

from cabal.config.home import default_master_key_path

log = structlog.get_logger(__name__)

#: Where the dev fallback key lives when no explicit path is given.


class MasterKeyError(RuntimeError):
    """Raised when the master key is unavailable or unusable."""


class EncryptedRowDamagedError(MasterKeyError):
    """The stored ciphertext is not a well-formed Fernet token at all.

    Separate from `MasterKeyMismatchError` because it sends the operator
    somewhere else entirely: no key restores a damaged row, so telling them to
    go find one burns the day. Prudence caught the collapse of these two on the
    #92 review — `Fernet.decrypt` raises `InvalidToken` for both, and the first
    version of the fix reported every one of them as a key mismatch.
    """


class MasterKeyMismatchError(MasterKeyError):
    """The key resolved fine, but it cannot decrypt what is already stored.

    Distinct from `MasterKeyError` on purpose: an unset or malformed key is a
    configuration mistake the operator can fix in place, while a MISMATCH means
    the key is valid and simply is not the one that wrote the rows. The remedy
    is different (restore the original key, or reset auth), so the error type
    that carries it is different too (cabal#92).
    """


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
        # TWO SOURCES THAT DISAGREE IS THE TRAP THAT CREATES cabal#92 MODE A.
        # The dev fallback writes <state_root>/data/.master_key while operators also export
        # the env var; whichever ran first encrypted the rows, and the other
        # silently wins on the next boot. Warn — do not "fix" it by preferring
        # the file, because the env var is the documented production source and
        # switching precedence would brick the opposite population.
        fallback = dev_fallback_path or default_master_key_path()
        if fallback.exists():
            try:
                on_disk = fallback.read_text().strip()
            except OSError:
                on_disk = ""
            if on_disk and on_disk != raw:
                log.warning(
                    "master_key.sources_differ",
                    path=str(fallback),
                    msg=(
                        "CABAL_MASTER_KEY and the on-disk dev key differ; the "
                        "environment variable wins. If cabal cannot decrypt its "
                        "stored keys, the rows were probably written under the "
                        "other one."
                    ),
                )
        return _validate_fernet_key(raw)

    if not allow_dev_fallback:
        raise MasterKeyError(
            "CABAL_MASTER_KEY is unset. Set it in the environment "
            "(generate with `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`) "
            "or pass --dev to enable the on-disk fallback."
        )

    path = dev_fallback_path or default_master_key_path()
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


def describe_master_key_source(
    *,
    env: dict[str, str] | None = None,
    dev_fallback_path: Path | None = None,
) -> str:
    """Human-readable name for WHERE the active master key came from.

    Exists so an error about a key mismatch can tell the operator which key
    it actually used — the single most useful fact when two of them exist
    (cabal#92). Mirrors `resolve_master_key`'s precedence; keep them in step.
    """
    env_map = env if env is not None else dict(os.environ)
    if env_map.get("CABAL_MASTER_KEY"):
        return "the CABAL_MASTER_KEY environment variable"
    path = dev_fallback_path or default_master_key_path()
    return f"the dev fallback file {path}"


def is_wellformed_fernet_token(blob: bytes) -> bool:
    """Whether *blob* is STRUCTURALLY a Fernet token, ignoring authentication.

    A Fernet token is urlsafe-base64 over `0x80 | 8-byte timestamp | 16-byte IV
    | ciphertext (16-byte blocks) | 32-byte HMAC`. All of that is checkable
    without the key, and it is what separates "wrong key" from "damaged row":
    the HMAC failing on a well-formed token means the key did not match, while
    a malformed envelope means the bytes themselves are wrong.

    Deliberately conservative — it answers only the question it can answer.
    Damage confined to the ciphertext body leaves the envelope intact and is
    indistinguishable from a wrong key without the key itself, which is why the
    mismatch message names both possibilities rather than promising one.
    """
    try:
        raw = base64.urlsafe_b64decode(blob)
    except Exception:  # noqa: BLE001 - any decode failure means malformed
        return False
    if len(raw) < 57 or raw[0] != 0x80:
        return False
    # 1 version + 8 timestamp + 16 IV + 32 HMAC = 57 bytes of envelope; the
    # remaining ciphertext is AES-CBC and therefore whole 16-byte blocks.
    return (len(raw) - 57) % 16 == 0
