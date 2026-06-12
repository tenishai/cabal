"""Tests for the cabal-shipped patterns handler (cabal#7).

End-to-end: a real techne DB seeded with a small patterns corpus is
queried via the PatternsHandler.dispatch() surface. Search returns
pattern entries (no-lift entries are filtered out). Record writes
through to the corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("limenal")
pytest.importorskip("techne")

from limenal.protocol import Context  # noqa: E402

from cabal.handlers.patterns import PatternsHandler  # noqa: E402


@pytest.fixture
def patterns_db(tmp_path: Path) -> Path:
    """A fresh techne DB seeded with three patterns + one no-lift entry."""
    from techne.core.database import init_db
    from techne.core.knowledge import store

    db_path = tmp_path / "patterns.db"
    init_db(db_path).close()

    store(
        db_path,
        content="# Capability probing with fallback dispatch\n\nProbe at startup; cache the verdict.",
        type="pattern",
        tags="surveyed:2026-06-09,domain:agent-memory",
        confidence="high",
        source="https://github.com/RohiRIK/OpenLtm",
    )
    store(
        db_path,
        content="# Hybrid rank fusion via RRF\n\nFuse BM25 and vector channels with reciprocal rank.",
        type="pattern",
        tags="surveyed:2026-06-12,domain:agent-memory",
        confidence="high",
        source="https://github.com/rohitg00/agentmemory",
    )
    store(
        db_path,
        content="# Bi-temporal knowledge model\n\nSeparate event_at from created_at.",
        type="pattern",
        tags="surveyed:2026-06-10,domain:agent-memory",
        confidence="medium",
        source="https://github.com/getzep/graphiti",
    )
    # A no-lift entry — must be filtered out of search results.
    store(
        db_path,
        content="not worth lifting",
        type="pattern",
        tags="surveyed-no-lift,surveyed:2026-06-12",
        confidence="low",
        source="https://github.com/skip/this",
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
    # The RRF pattern entry should match
    found = any("Hybrid rank fusion" in h["content"] for h in hits)
    assert found


@pytest.mark.asyncio
async def test_search_filters_out_no_lift_entries(patterns_db: Path) -> None:
    """No-lift entries are dedup signals, NOT recall targets."""
    handler = PatternsHandler()
    result = await handler.dispatch("search", {"query": "lifting"}, _ctx(patterns_db))
    assert result.is_error is False
    hits = result.content[0]["data"]["hits"]
    for h in hits:
        assert "surveyed-no-lift" not in (h.get("tags") or "")


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
async def test_search_attaches_effective_confidence(patterns_db: Path) -> None:
    """Each hit should include effective_confidence (decay-adjusted)."""
    handler = PatternsHandler()
    result = await handler.dispatch(
        "search", {"query": "rank fusion"}, _ctx(patterns_db)
    )
    assert result.is_error is False
    hits = result.content[0]["data"]["hits"]
    for h in hits:
        # half_life defaults to 0 (decay disabled) — effective should equal base.
        # 'high' confidence → 0.9
        if h.get("confidence") == "high":
            assert h["effective_confidence"] == pytest.approx(0.9)


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
    assert isinstance(payload["entry_id"], int)

    # Round-trip — search should now find it
    found = await handler.dispatch(
        "search", {"query": "interesting way"}, _ctx(patterns_db)
    )
    found_payload = found.content[0]["data"]
    assert any(
        "Some new pattern" in h["content"] for h in found_payload["hits"]
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
