"""The vendored liminal.protocol (tests/_vendor) must match the real one (cabal#359)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

VENDORED = Path(__file__).parent / "_vendor" / "liminal" / "protocol"


def _real_protocol_dir() -> Path | None:
    spec = importlib.util.find_spec("liminal.protocol")
    if spec is None or spec.origin is None:
        return None
    path = Path(spec.origin).parent
    return None if path == VENDORED else path


@pytest.mark.parametrize("name", sorted(p.name for p in VENDORED.glob("*.py")))
def test_vendored_protocol_matches_installed_liminal(name: str) -> None:
    real = _real_protocol_dir()
    if real is None:
        pytest.skip("no real liminal installed; the integration job runs this")
    assert (VENDORED / name).read_text() == (real / name).read_text(), (
        f"tests/_vendor/liminal/protocol/{name} differs from liminal's; re-copy it "
        "and bump VENDORED_FROM"
    )
