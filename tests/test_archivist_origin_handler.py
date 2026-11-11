"""cabal#410 at the handler: origin, access labels, caller ranking, lineage."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("liminal.protocol")

from liminal.protocol import Context  # noqa: E402

from cabal.archivist.erase import erase  # noqa: E402
from cabal.handlers.archivist import ArchivistConfig, ArchivistHandler, get_handler  # noqa: E402


def _ctx() -> Context:
    return Context(identity="anonymous", agent_name="anonymous", credentials={}, extra={})


@pytest.fixture
def handler(tmp_path: Path) -> ArchivistHandler:
    return ArchivistHandler(default_config=ArchivistConfig(db_path=tmp_path / "a.db", name="mash1"))


async def _publish(handler, **payload):
    base = {"resource_name": "r", "summary": "s", "entry_type": "fact"}
    return await handler.dispatch("publish", {**base, **payload}, _ctx())


@pytest.mark.asyncio
async def test_archivist_comes_from_the_server_and_origin_from_the_caller(handler) -> None:
    pub = await _publish(handler, origin={"cabal": "techne-next", "agent": "prudence", "role": "reviewer"})
    assert pub.is_error is False
    got = await handler.dispatch("get", {"entry_id": pub.content[0]["data"]["entry_id"]}, _ctx())
    entry = got.content[0]["data"]["entry"]
    assert entry["origin"] == {"archivist": "mash1", "cabal": "techne-next", "agent": "prudence", "role": "reviewer"}
    assert entry["access"] == "internal"


@pytest.mark.asyncio
async def test_caller_cannot_set_the_archivist(handler) -> None:
    pub = await _publish(handler, origin={"archivist": "elsewhere"})
    assert pub.is_error is True
    assert "unknown keys ['archivist']" in pub.error


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["cabal", "role"])
async def test_unenforced_access_labels_are_refused(handler, label) -> None:
    pub = await _publish(handler, access=label)
    assert pub.is_error is True
    assert "not enforced yet" in pub.error


@pytest.mark.asyncio
async def test_public_label_is_kept_and_unknown_refused(handler) -> None:
    assert (await _publish(handler, access="public")).is_error is False
    bad = await _publish(handler, resource_name="x", access="secret")
    assert bad.is_error is True


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["techne-next", {"cabal": 3}])
async def test_bad_origin_is_refused(handler, origin) -> None:
    assert (await _publish(handler, origin=origin)).is_error is True


@pytest.mark.asyncio
async def test_search_ranks_by_caller(handler) -> None:
    await _publish(handler, resource_name="a", summary="flaky flaky flaky", origin={"cabal": "other"})
    mine = await _publish(handler, resource_name="b", summary="flaky", origin={"cabal": "mine"})
    res = await handler.dispatch("search", {"query": "flaky", "caller": {"cabal": "mine"}}, _ctx())
    hits = res.content[0]["data"]["hits"]
    assert hits[0]["id"] == mine.content[0]["data"]["entry_id"]
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_get_reports_lineage_and_erased_publish_is_an_outcome(handler, tmp_path) -> None:
    pub = await _publish(handler, refs=["src:9"], derived_from=["src:8"])
    eid = pub.content[0]["data"]["entry_id"]
    got = await handler.dispatch("get", {"entry_id": eid}, _ctx())
    assert got.content[0]["data"]["derived_from"] == ["src:8", "src:9"]

    erase(tmp_path / "a.db", "src:8", apply=True)
    again = await _publish(handler, resource_name="new", summary="other", derived_from=["src:8"])
    assert again.is_error is False
    assert again.content[0]["data"]["outcome"] == "refused_erased"
    assert again.content[0]["data"]["related_ids"] == ["src:8"]


def test_get_handler_reads_the_archivist_name(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CABAL_ARCHIVIST_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("CABAL_ARCHIVIST_NAME", " mash1 ")
    h = get_handler()
    assert h.default_config.name == "mash1"
    monkeypatch.delenv("CABAL_ARCHIVIST_DB")
    assert get_handler().default_config.db_path is None
