"""Archivist search takes any natural-language query (cabal#356).

Store-level only, with no liminal import, so these run in the unit CI job
(tests/test_archivist.py skips there; see cabal#359).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cabal.archivist import ArchivistStore, Entry


@pytest.fixture
def store(tmp_path: Path) -> ArchivistStore:
    s = ArchivistStore(tmp_path / "archivist.db")
    s.publish(Entry(
        resource_name="host-a",
        summary="The build host is host-a.local; run docker/compose there.",
        entry_type="fact",
    ))
    s.publish(Entry(
        resource_name="host-b",
        summary="host-b is not the build host.",
        entry_type="fact",
    ))
    return s


# Every query here raised sqlite3.OperationalError before the fix.
@pytest.mark.parametrize(
    "query",
    [
        "what's the build host?",
        "host-a.local",
        "docker/compose",
        "c++ build",
        "host, build",
        "x=y",
        "a;b",
        "100%",
        "a@b",
        "price $5",
        "a&b",
        "a|b",
        "hello?",
        "hello!",
        "hello.",
        "foo AND",
        "AND",
        "NOT host-a",
        "host-a OR",
        "{host-a}",
        "?",
    ],
)
def test_query_never_raises(store: ArchivistStore, query: str) -> None:
    assert isinstance(store.search(query), list)


@pytest.mark.parametrize(
    "query",
    ["host-a.local", "docker/compose", "HOST-A", "host-a host", "host-a host?"],
)
def test_punctuated_query_finds_the_entry(store: ArchivistStore, query: str) -> None:
    hits = store.search(query)
    assert [h.resource_name for h in hits][:1] == ["host-a"]


def test_tokens_are_and_ed(store: ArchivistStore) -> None:
    # Both entries mention "build host"; only one also says "docker".
    assert {h.resource_name for h in store.search("build host")} == {"host-a", "host-b"}
    assert {h.resource_name for h in store.search("build host?")} == {"host-a", "host-b"}
    assert [h.resource_name for h in store.search("build docker")] == ["host-a"]


def test_operators_are_plain_words(store: ArchivistStore) -> None:
    # "NOT host-b" used to be an FTS5 NOT; now "not" is a word host-b's row has.
    assert [h.resource_name for h in store.search("NOT host-b")] == ["host-b"]
    # "host-a OR host-b" used to match both; now "or" must appear as a word.
    assert store.search("host-a OR host-b") == []


def test_embedded_double_quote(store: ArchivistStore) -> None:
    assert isinstance(store.search('say "build" host'), list)
    assert isinstance(store.search('"'), list)


# ---- control characters and lone surrogates (#365) ----------------------------


@pytest.mark.parametrize(
    "query", ["\x00", "build\x00host", "\ud800", "host \ud800", "\x1b[31mhost", "\x7f"]
)
def test_control_characters_and_lone_surrogates_never_raise(
    store: ArchivistStore, query: str
) -> None:
    assert isinstance(store.search(query), list)


def test_a_nul_separates_words_like_a_space(store: ArchivistStore) -> None:
    assert [h.resource_name for h in store.search("docker\x00compose")] == ["host-a"]


def test_a_surrogate_pair_as_two_code_points_is_one_character() -> None:
    from cabal.archivist.store import _clean_query

    assert _clean_query("deploy \ud83d\ude80") == "deploy \U0001f680"
    assert _clean_query("x\ud800y") == "x\ufffdy"


# ---- tag filter applies before the limit (#362) -------------------------------


def test_tag_filter_finds_a_row_ranked_below_many_others(tmp_path: Path) -> None:
    # The issue's reproduction: 50 staging rows and 1 production row match "cache".
    s = ArchivistStore(tmp_path / "a.db")
    for i in range(50):
        s.publish(Entry(resource_name=f"stage-{i}", summary=f"cache node {i} cache",
                        entry_type="fact", tags=["staging"]))
    s.publish(Entry(resource_name="prod", summary="the cache tier", entry_type="fact",
                    tags=["production"]))
    hits = s.search("cache", tags=["production"], top_k=10)
    assert [h.resource_name for h in hits] == ["prod"]


def test_every_tag_must_match_and_top_k_holds(tmp_path: Path) -> None:
    s = ArchivistStore(tmp_path / "a.db")
    for i in range(5):
        s.publish(Entry(resource_name=f"both-{i}", summary=f"cache {i}", entry_type="fact",
                        tags=["team", "host-a"]))
    s.publish(Entry(resource_name="one", summary="cache x", entry_type="fact", tags=["team"]))
    hits = s.search("cache", tags=["team", "host-a"], top_k=3)
    assert len(hits) == 3
    assert all(h.resource_name.startswith("both-") for h in hits)
