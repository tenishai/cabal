"""`cbl guide` and the guides it ships.

The guides are package data. A guide that exists in the checkout but is missing
from the wheel works here and fails for every installed user, so the manifest is
checked against both the files on disk and the package-data globs.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cabal.cli.main import app
from cabal.guides import MANIFEST, guide_text

REPO = Path(__file__).resolve().parent.parent
GUIDES = REPO / "src" / "cabal" / "guides"


def _package_data_globs() -> list[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]["cabal"]


def test_every_manifest_entry_is_a_real_file():
    for name in MANIFEST:
        assert (GUIDES / f"{name}.md").is_file(), name


def test_every_guide_file_is_in_the_manifest():
    on_disk = {p.stem for p in GUIDES.glob("*.md")}
    assert on_disk == set(MANIFEST)


def test_every_guide_is_in_the_wheel_package_data():
    globs = _package_data_globs()
    for name in MANIFEST:
        rel = f"guides/{name}.md"
        assert any(fnmatch.fnmatch(rel, g) for g in globs), f"{rel} not in package-data {globs}"


def test_welcome_is_substantial():
    assert len(guide_text("welcome").splitlines()) >= 120


def test_unknown_or_hostile_names_raise():
    for bad in ("nope", "../../pyproject", "/etc/passwd", "", "welcome.md"):
        with pytest.raises(KeyError):
            guide_text(bad)


def test_cli_lists_guides():
    result = CliRunner().invoke(app, ["guide"])
    assert result.exit_code == 0, result.output
    for name, desc in MANIFEST.items():
        assert name in result.output
        assert desc in result.output


def test_cli_prints_a_guide():
    result = CliRunner().invoke(app, ["guide", "welcome"])
    assert result.exit_code == 0, result.output
    assert result.output == guide_text("welcome")


def test_cli_unknown_name_fails_and_lists_valid_names():
    result = CliRunner().invoke(app, ["guide", "nope"])
    assert result.exit_code == 2
    assert "welcome" in result.output
