"""Handler status() for liminal's /healthz (tenishai/liminal#191).

liminal calls a handler's optional status() and shows a non-None answer on
/healthz instead of "ready". Each cabal handler refuses every call that has no
corpus, so with no default db it must say so, naming what to set.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytest.importorskip("liminal.protocol")

# (module, env var get_handler reads, or None when it reads none)
HANDLERS = [
    ("archivist", "CABAL_ARCHIVIST_DB"),
    ("documents", "CABAL_LIBRARIAN_DB"),
    ("patterns", "CABAL_ARCHITECT_DB"),
    ("code", None),
]

ALL_ENV = [env for _, env in HANDLERS if env]


@pytest.fixture(autouse=True)
def _no_corpus_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in ALL_ENV:
        monkeypatch.delenv(env, raising=False)


@pytest.mark.parametrize(("module", "env"), HANDLERS)
def test_unconfigured_handler_says_what_is_missing(module: str, env: str | None) -> None:
    handler = importlib.import_module(f"cabal.handlers.{module}").get_handler()
    status = handler.status()
    assert status is not None
    assert status.startswith("unconfigured: ")
    assert f"{module}.db_path" in status
    if env:
        assert f"{env} is unset" in status


@pytest.mark.parametrize(("module", "env"), [h for h in HANDLERS if h[1]])
def test_env_configured_handler_reports_ready(
    module: str, env: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(env, str(tmp_path / "corpus.db"))
    handler = importlib.import_module(f"cabal.handlers.{module}").get_handler()
    assert handler.status() is None


def test_code_handler_with_a_default_db_reports_ready(tmp_path: Path) -> None:
    from cabal.handlers.code import CodeHandler, CodexConfig

    assert CodeHandler(CodexConfig(db_path=tmp_path / "codex.db")).status() is None
