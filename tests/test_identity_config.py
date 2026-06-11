"""identity.toml loader tests."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cabal.config.identity import IdentityConfig, load_identity


def test_minimal_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text("[cabal]\nport = 7500\n")
    cfg = load_identity(cfg_path)
    assert cfg.cabal.port == 7500
    assert cfg.cabal.host == "127.0.0.1"  # default


def test_full_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text(
        dedent(
            """
            [cabal]
            host = "0.0.0.0"
            port = 7400
            data_dir = "data"

            [personas.rook]
            description = "Engineering primus"
            role = "primus"
            safety_tier = "strict"

            [personas.rook.subordinates.implementer]
            model = "anthropic/claude-sonnet-4-6"
            max_tokens = 16384

            [personas.archivist]
            description = "Memory and search"
            role = "singleton"
            safety_tier = "moderate"

            [litellm]
            api_base = "http://litellm:4000"
            api_key_env = "LITELLM_MASTER_KEY"
            """
        )
    )
    cfg: IdentityConfig = load_identity(cfg_path)
    assert cfg.cabal.host == "0.0.0.0"
    assert set(cfg.personas.keys()) == {"rook", "archivist"}
    assert cfg.personas["rook"].role == "primus"
    sub = cfg.personas["rook"].subordinates["implementer"]
    assert sub.model == "anthropic/claude-sonnet-4-6"
    assert sub.max_tokens == 16384
    assert cfg.litellm.api_base == "http://litellm:4000"


def test_unknown_section_warns(tmp_path: Path, capsys) -> None:
    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text("[mystery]\nx = 1\n")
    load_identity(cfg_path)
    captured = capsys.readouterr()
    assert "unknown_section" in captured.out or "mystery" in captured.out


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_identity(tmp_path / "nope.toml")
