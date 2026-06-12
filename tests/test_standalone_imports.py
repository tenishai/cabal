"""Standalone-evaluation tests: cabal imports cleanly without optional extras.

cabal#1 mandates that `import cabal` (and importing any cabal submodule)
must NOT require limenal or techne to be installed. Personas with [mcp]
or [memory] config still need them, but the cabal core itself is
backend-agnostic.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest


@pytest.fixture
def block_limenal(monkeypatch):
    """Make any future `import limenal` fail. Existing references stay valid."""
    # Wipe limenal from sys.modules so a fresh import is required.
    for mod in list(sys.modules):
        if mod == "limenal" or mod.startswith("limenal."):
            del sys.modules[mod]
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "limenal" or name.startswith("limenal."):
            raise ImportError(f"limenal blocked in test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_import_cabal_without_limenal(block_limenal) -> None:
    """`import cabal` must succeed even when limenal is not installed."""
    if "cabal" in sys.modules:
        del sys.modules["cabal"]
    importlib.import_module("cabal")


def test_import_mcp_proxy_module_without_limenal(block_limenal) -> None:
    """The mcp_proxy module itself must be importable even without limenal.
    Only the runtime helpers raise ImportError when actually invoked."""
    # Wipe + reimport so the module's top-level code re-runs under the block
    for mod in list(sys.modules):
        if mod.endswith("mcp_proxy") or mod.endswith("tools"):
            del sys.modules[mod]
    importlib.import_module("cabal.core.tools.mcp_proxy")


def test_build_proxy_raises_clear_error_without_limenal(block_limenal) -> None:
    """The runtime helper points operators at the right install command."""
    for mod in list(sys.modules):
        if mod.endswith("mcp_proxy"):
            del sys.modules[mod]
    mp = importlib.import_module("cabal.core.tools.mcp_proxy")
    with pytest.raises(ImportError, match="pip install cabal\\[mcp\\]"):
        mp.build_proxy(
            base_url="http://test",
            persona="rook",
            agent_name="rook-primus",
            token_provider=lambda: "tok",
        )
