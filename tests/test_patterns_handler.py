"""Tests for the cabal-shipped patterns handler (cabal#7).

End-to-end: a real techne DB seeded with a small patterns corpus is
queried via the PatternsHandler.dispatch() surface. Search returns
pattern entries (no-lift entries are filtered out). Record writes
through to the corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("liminal.protocol")
pytest.importorskip("techne")

from liminal.protocol import Context  # noqa: E402

from cabal.handlers.patterns import PatternsHandler  # noqa: E402

SKIP_URL = "https://github.com/skip/this"


@pytest.fixture
def patterns_db(tmp_path: Path) -> Path:
    """A fresh architect.db seeded with three patterns + one no-lift investigation."""
    from techne.core.architect import ARCHITECT_SCHEMA
    from techne.core.patterns import add_pattern, upsert_investigation
    from techne.core.stores import SqliteStore

    db_path = tmp_path / "architect.db"
    SqliteStore("architect", db_path).migrate(ARCHITECT_SCHEMA)

    def _add(url: str, name: str, body: str, confidence: str) -> None:
        inv = upsert_investigation(
            db_path, url=url, surveyed_at="2026-06-09", verdict="full-eval"
        )
        add_pattern(
            db_path,
            investigation_id=inv,
            name=name,
            source_url=url,
            body=body,
            confidence=confidence,
        )

    _add(
        "https://github.com/RohiRIK/OpenLtm",
        "Capability probing with fallback dispatch",
        "Probe at startup; cache the verdict.",
        "high",
    )
    _add(
        "https://github.com/rohitg00/agentmemory",
        "Hybrid rank fusion via RRF",
        "Fuse BM25 and vector channels with reciprocal rank.",
        "high",
    )
    _add(
        "https://github.com/getzep/graphiti",
        "Bi-temporal knowledge model",
        "Separate event_at from created_at.",
        "medium",
    )
    # A no-lift entry — a skip-with-flag investigation with NO pattern row, so it
    # must never surface in search_patterns (which only reads active patterns).
    upsert_investigation(
        db_path, url=SKIP_URL, surveyed_at="2026-06-12", verdict="skip-with-flag"
    )
    return db_path


def _ctx(db_path: Path) -> Context:
    return Context(
        identity="architect",
        agent_name="architect",
        credentials={},
        extra={"patterns.db_path": str(db_path)},
    )


# ---- search ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_pattern_entries(patterns_db: Path) -> None:
    handler = PatternsHandler()
    result = await handler.dispatch("search", {"query": "rank fusion"}, _ctx(patterns_db))
    assert result.is_error is False
    payload = result.content[0]["data"]
    hits = payload["hits"]
    assert len(hits) >= 1
    # The RRF pattern entry should match (title lives in `name`).
    found = any("Hybrid rank fusion" in h["name"] for h in hits)
    assert found


@pytest.mark.asyncio
async def test_search_filters_out_no_lift_entries(patterns_db: Path) -> None:
    """No-lift entries are skip-with-flag investigations with no pattern row,
    so they are never recall targets."""
    handler = PatternsHandler()
    result = await handler.dispatch("search", {"query": "lifting"}, _ctx(patterns_db))
    assert result.is_error is False
    hits = result.content[0]["data"]["hits"]
    assert all(h.get("source_url") != SKIP_URL for h in hits)


@pytest.mark.asyncio
async def test_search_respects_top_k(patterns_db: Path) -> None:
    handler = PatternsHandler()
    # Query likely to hit multiple entries — limit to 1
    result = await handler.dispatch(
        "search", {"query": "pattern", "top_k": 1}, _ctx(patterns_db)
    )
    assert result.is_error is False
    hits = result.content[0]["data"]["hits"]
    assert len(hits) <= 1


@pytest.mark.asyncio
async def test_search_attaches_confidence_and_score(patterns_db: Path) -> None:
    """Each hit carries the stored confidence label and a BM25 relevance score."""
    handler = PatternsHandler()
    result = await handler.dispatch(
        "search", {"query": "rank fusion"}, _ctx(patterns_db)
    )
    assert result.is_error is False
    hits = result.content[0]["data"]["hits"]
    assert len(hits) >= 1
    for h in hits:
        assert h["confidence"] in {"high", "medium", "low"}
        assert "score" in h


@pytest.mark.asyncio
async def test_search_rejects_empty_query(patterns_db: Path) -> None:
    handler = PatternsHandler()
    result = await handler.dispatch("search", {"query": "   "}, _ctx(patterns_db))
    assert result.is_error is True


@pytest.mark.asyncio
async def test_search_returns_clear_failure_without_db(tmp_path: Path) -> None:
    """No db_path → structured failure, not an exception."""
    handler = PatternsHandler()
    ctx = Context(identity="x", agent_name="x", credentials={}, extra={})
    result = await handler.dispatch("search", {"query": "anything"}, ctx)
    assert result.is_error is True
    assert "no patterns corpus" in result.error.lower()


# ---- record ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_writes_to_corpus(patterns_db: Path) -> None:
    handler = PatternsHandler()
    result = await handler.dispatch(
        "record",
        {
            "name": "Some new pattern",
            "source_url": "https://github.com/example/repo",
            "what": "Does a thing in an interesting way.",
            "why": "Less code than alternatives.",
            "where_to_apply": "future-thing.py:42",
        },
        _ctx(patterns_db),
    )
    assert result.is_error is False
    payload = result.content[0]["data"]
    assert payload["name"] == "Some new pattern"
    assert payload["source_url"] == "https://github.com/example/repo"
    assert isinstance(payload["pattern_id"], str)
    assert payload["stored"] is True

    # Round-trip — search should now find it (FTS matches the body prose).
    found = await handler.dispatch(
        "search", {"query": "interesting way"}, _ctx(patterns_db)
    )
    found_payload = found.content[0]["data"]
    assert any(
        "Some new pattern" in h["name"] for h in found_payload["hits"]
    )


@pytest.mark.asyncio
async def test_record_rejects_missing_fields(patterns_db: Path) -> None:
    handler = PatternsHandler()
    result = await handler.dispatch(
        "record",
        {
            "name": "Half-baked",
            "source_url": "https://github.com/x/y",
            # missing: what / why / where_to_apply
        },
        _ctx(patterns_db),
    )
    assert result.is_error is True


# ---- traversal defense ---------------------------------------------------


@pytest.mark.asyncio
async def test_db_path_rejects_traversal(patterns_db: Path) -> None:
    handler = PatternsHandler()
    ctx = Context(
        identity="x", agent_name="x", credentials={},
        extra={"patterns.db_path": "../../etc/passwd"},
    )
    result = await handler.dispatch("search", {"query": "x"}, ctx)
    assert result.is_error is True


# ---- unknown tool -------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_failure(patterns_db: Path) -> None:
    handler = PatternsHandler()
    result = await handler.dispatch("ghost", {}, _ctx(patterns_db))
    assert result.is_error is True
    assert "unknown tool" in result.error.lower()


# ---- entry-point + handler shape ----------------------------------------


def test_get_handler_factory_returns_patterns_handler() -> None:
    from cabal.handlers.patterns import get_handler
    h = get_handler()
    assert isinstance(h, PatternsHandler)


def test_get_handler_reads_env_db_path(monkeypatch, tmp_path: Path) -> None:
    """CABAL_ARCHITECT_DB seeds default_config.db_path; unset/empty → None.

    This is the env-config branch that lets `liminal serve` point the patterns
    handler at a fixed architect.db — the reason the commit exists (Prudence #101).
    """
    from cabal.handlers.patterns import get_handler

    db = tmp_path / "architect.db"
    monkeypatch.setenv("CABAL_ARCHITECT_DB", str(db))
    assert get_handler().default_config.db_path == db

    # empty string is falsy → no db_path
    monkeypatch.setenv("CABAL_ARCHITECT_DB", "")
    assert get_handler().default_config.db_path is None

    # unset → None
    monkeypatch.delenv("CABAL_ARCHITECT_DB", raising=False)
    assert get_handler().default_config.db_path is None


def test_capability_card_has_required_fields() -> None:
    handler = PatternsHandler()
    card = handler.capability_card()
    assert card["handler"] == "patterns"
    tool_names = {t["name"] for t in card["tools"]}
    assert tool_names == {"search", "record"}


def test_tools_advertises_correct_input_schemas() -> None:
    handler = PatternsHandler()
    tools = handler.tools()
    by_name = {t.name: t for t in tools}
    assert by_name["search"].input_schema["required"] == ["query"]
    record_required = set(by_name["record"].input_schema["required"])
    assert record_required == {"name", "source_url", "what", "why", "where_to_apply"}
