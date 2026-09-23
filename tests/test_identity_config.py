"""identity.toml loader tests."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import structlog

from cabal.config.identity import (
    IdentityConfig,
    load_identity,
    resolve_data_dir,
    resolve_under_data_dir,
)


def test_resolve_data_dir_relative_anchors_to_identity_dir(tmp_path: Path) -> None:
    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text('[cabal]\ndata_dir = "data"\n')
    cfg = load_identity(cfg_path)
    assert resolve_data_dir(cfg, cfg_path) == tmp_path / "data"


def test_resolve_data_dir_absolute_used_as_is(tmp_path: Path) -> None:
    abs_dir = tmp_path / "elsewhere" / "root"
    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text(f'[cabal]\ndata_dir = "{abs_dir}"\n')
    cfg = load_identity(cfg_path)
    assert resolve_data_dir(cfg, cfg_path) == abs_dir


def test_resolve_data_dir_defaults_when_no_identity(tmp_path: Path) -> None:
    # No identity → CabalConfig default "data", anchored at the config dir.
    cfg_path = tmp_path / "identity.toml"
    assert resolve_data_dir(None, cfg_path) == tmp_path / "data"


def test_resolve_data_dir_bare_filename_anchors_at_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bare filename ("identity.toml", e.g. passed via --config) has the CWD as
    # its parent, which is where that file is actually read from — so data still
    # co-locates with the identity file (Prudence, PR #22). The app no longer
    # defaults to a bare filename; see cabal.config.home (#211).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "identity.toml").write_text('[cabal]\ndata_dir = "data"\n')
    cfg = load_identity("identity.toml")  # bare filename, like the app default
    assert resolve_data_dir(cfg, "identity.toml") == Path.cwd() / "data"


def test_resolve_under_data_dir_relative_and_absolute(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    assert resolve_under_data_dir("techne.db", root) == root / "techne.db"
    abs_p = tmp_path / "x" / "techne.db"
    assert resolve_under_data_dir(str(abs_p), root) == abs_p


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

            [personas.mason]
            description = "Engineering primus"
            role = "primus"
            safety_tier = "strict"

            [personas.mason.subordinates.implementer]
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
    assert set(cfg.personas.keys()) == {"mason", "archivist"}
    assert cfg.personas["mason"].role == "primus"
    sub = cfg.personas["mason"].subordinates["implementer"]
    assert sub.model == "anthropic/claude-sonnet-4-6"
    assert sub.max_tokens == 16384
    assert cfg.litellm.api_base == "http://litellm:4000"


def test_unknown_section_warns(tmp_path: Path) -> None:
    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text("[mystery]\nx = 1\n")
    # capture_logs records structlog events directly, independent of the global
    # sink (stdout/stderr) — capsys was order-dependent because structlog binds
    # its stream at configure-once time, which capsys can't intercept later.
    with structlog.testing.capture_logs() as logs:
        load_identity(cfg_path)
    assert any(
        entry.get("event") == "identity.unknown_section"
        and entry.get("section") == "mystery"
        for entry in logs
    )


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_identity(tmp_path / "nope.toml")


def test_data_dir_env_wins_over_the_identity_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#214: the image sets CABAL_DATA_DIR=/data so a container keeps its stores on
    the volume even when the mounted identity file says `data_dir = "data"`."""
    from cabal.config.identity import DATA_DIR_ENV

    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text('[cabal]\ndata_dir = "data"\n')
    cfg = load_identity(cfg_path)
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "volume"))
    assert resolve_data_dir(cfg, cfg_path) == tmp_path / "volume"


def test_a_relative_data_dir_env_anchors_like_the_identity_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cabal.config.identity import DATA_DIR_ENV

    # In a subdirectory, because the autouse fixture chdirs to tmp_path: with the
    # file there too, anchoring at the CWD would pass as well (Prudence, #217 r1).
    cfg_path = tmp_path / "cfg" / "identity.toml"
    monkeypatch.setenv(DATA_DIR_ENV, "elsewhere")
    assert resolve_data_dir(None, cfg_path) == tmp_path / "cfg" / "elsewhere"


def test_an_unset_data_dir_env_leaves_the_identity_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cabal.config.identity import DATA_DIR_ENV

    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    cfg_path = tmp_path / "identity.toml"
    cfg_path.write_text('[cabal]\ndata_dir = "mine"\n')
    assert resolve_data_dir(load_identity(cfg_path), cfg_path) == tmp_path / "mine"
