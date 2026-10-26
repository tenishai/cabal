"""A superseded entry fetched by id says it is retired and what replaced it
(cabal#357). Store-level, with no liminal import, so the unit CI job runs it;
the handler-level check is in tests/test_archivist.py."""

from __future__ import annotations

from pathlib import Path

from cabal.archivist import ArchivistStore, Entry


def test_superseded_by_names_the_replacement(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "archivist.db")
    old = store.publish(Entry(resource_name="mash1", summary="mash1 is at .15", entry_type="fact"))
    new = store.publish(
        Entry(resource_name="mash1", summary="mash1 is at .16", entry_type="fact"),
        supersede=True,
    )

    assert store.get(old.entry_id).status == "superseded"
    assert store.superseded_by(old.entry_id) == [new.entry_id]
    assert store.superseded_by(new.entry_id) == []


def test_chain_is_one_hop_per_entry(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "archivist.db")
    a = store.publish(Entry(resource_name="r", summary="v1", entry_type="fact"))
    b = store.publish(Entry(resource_name="r", summary="v2", entry_type="fact"), supersede=True)
    c = store.publish(Entry(resource_name="r", summary="v3", entry_type="fact"), supersede=True)

    assert store.superseded_by(a.entry_id) == [b.entry_id]
    assert store.superseded_by(b.entry_id) == [c.entry_id]


def test_a_related_edge_is_not_a_supersede(tmp_path: Path) -> None:
    # A plain publish over an existing entry for the same resource writes a
    # RELATED edge new -> prior, which is also an inbound edge on the prior.
    # Only SUPERSEDES edges may name a replacement (review of PR #366).
    store = ArchivistStore(tmp_path / "archivist.db")
    first = store.publish(Entry(resource_name="mash1", summary="mash1 runs builds", entry_type="fact"))
    second = store.publish(Entry(resource_name="mash1", summary="mash1 has 61 GB", entry_type="fact"))

    assert second.outcome == "related"
    assert store.get(first.entry_id).status == "active"
    assert store.superseded_by(first.entry_id) == []
