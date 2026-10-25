"""Caught exceptions are logged without frame locals (cabal#370).

Store- and helper-level, with no liminal import beyond the vendored protocol, so
these run in unit CI.
"""

from __future__ import annotations

import io

import pytest
import structlog

from cabal.core.observability.failures import log_failure

SECRET = "s3cret-value-that-must-not-be-logged"


@pytest.fixture
def rich_console():
    """structlog's default when rich is installed: tracebacks with show_locals."""
    pytest.importorskip("rich")
    out = io.StringIO()
    structlog.configure(
        processors=[structlog.dev.ConsoleRenderer(colors=False)],
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )
    yield out
    structlog.reset_defaults()


def _fails(token: str) -> None:
    raise ValueError("boom")


def test_log_exception_would_print_the_locals(rich_console):
    """The hazard, measured: a frame local reaches the log through log.exception."""
    log = structlog.get_logger()
    try:
        _fails(SECRET)
    except ValueError:
        log.exception("x")
    assert SECRET in rich_console.getvalue()


def test_log_failure_keeps_the_stack_and_drops_the_locals(rich_console):
    log = structlog.get_logger()
    try:
        _fails(SECRET)
    except ValueError:
        log_failure(log, "x", tool="t")
    text = rich_console.getvalue()
    assert SECRET not in text
    assert "ValueError: boom" in text
    assert "_fails" in text


@pytest.mark.asyncio
async def test_archivist_unexpected_error_logs_no_payload(rich_console, tmp_path, monkeypatch):
    pytest.importorskip("liminal.protocol")
    from liminal.protocol import Context

    from cabal.archivist import ArchivistStore
    from cabal.handlers.archivist import ArchivistHandler

    def boom(self, *a, **k):
        raise RuntimeError("store broke")

    monkeypatch.setattr(ArchivistStore, "publish", boom)
    handler = ArchivistHandler()
    ctx = Context(
        identity="i", agent_name="a", credentials={"TOKEN": SECRET},
        extra={"archivist.db_path": str(tmp_path / "a.db")},
    )
    result = await handler.dispatch(
        "publish",
        {"resource_name": "r", "summary": "s", "entry_type": "fact", "details": SECRET},
        ctx,
    )
    assert result.is_error
    text = rich_console.getvalue()
    assert "RuntimeError: store broke" in text
    assert SECRET not in text


def test_hide_password():
    from cabal.core.app import _hide_password

    assert _hide_password("postgresql+psycopg://u:pw@db:5432/c") == "postgresql+psycopg://u:***@db:5432/c"
    assert _hide_password("sqlite:///data/cabal.db") == "sqlite:///data/cabal.db"
