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
        resource_name="mash1",
        summary="The build host is mash1.local; run docker/compose there.",
        entry_type="fact",
    ))
    s.publish(Entry(
        resource_name="dev04",
        summary="dev04 is not the build host.",
        entry_type="fact",
    ))
    return s


# Every query here raised sqlite3.OperationalError before the fix.
@pytest.mark.parametrize(
    "query",
    [
        "what's the build host?",
        "mash1.local",
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
        "NOT mash1",
        "mash1 OR",
        "{mash1}",
        "?",
    ],
)
def test_query_never_raises(store: ArchivistStore, query: str) -> None:
    assert isinstance(store.search(query), list)


@pytest.mark.parametrize(
    "query",
    ["mash1.local", "docker/compose", "MASH1", "mash1 host", "mash1 host?"],
)
def test_punctuated_query_finds_the_entry(store: ArchivistStore, query: str) -> None:
    hits = store.search(query)
    assert [h.resource_name for h in hits][:1] == ["mash1"]


def test_tokens_are_and_ed(store: ArchivistStore) -> None:
    # Both entries mention "build host"; only one also says "docker".
    assert {h.resource_name for h in store.search("build host")} == {"mash1", "dev04"}
    assert {h.resource_name for h in store.search("build host?")} == {"mash1", "dev04"}
    assert [h.resource_name for h in store.search("build docker")] == ["mash1"]


def test_operators_are_plain_words(store: ArchivistStore) -> None:
    # "NOT dev04" used to be an FTS5 NOT; now "not" is a word dev04's row has.
    assert [h.resource_name for h in store.search("NOT dev04")] == ["dev04"]
    # "mash1 OR dev04" used to match both; now "or" must appear as a word.
    assert store.search("mash1 OR dev04") == []


def test_embedded_double_quote(store: ArchivistStore) -> None:
    assert isinstance(store.search('say "build" host'), list)
    assert isinstance(store.search('"'), list)
