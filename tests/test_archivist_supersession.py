"""Deterministic same-resource supersession in the Archivist (agent-model §5e).

Store-level only — no limenal import, so these run in the no-siblings CI job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cabal.archivist import ArchivistStore, Entry


@pytest.fixture
def store(tmp_path: Path) -> ArchivistStore:
    return ArchivistStore(tmp_path / "archivist.db")


def _entry(summary: str, *, resource: str = "auth-service", etype: str = "decision") -> Entry:
    return Entry(resource_name=resource, summary=summary, entry_type=etype)


def test_supersede_retires_prior_and_links(store: ArchivistStore) -> None:
    a = store.publish(_entry("auth uses jwt alpha"))
    assert a.outcome == "new"
    b = store.publish(_entry("auth uses jwt beta"), supersede=True)
    assert b.outcome == "superseded"
    assert a.entry_id in b.related_ids

    assert store.get(a.entry_id).status == "superseded"
    assert store.get(b.entry_id).status == "active"

    rels = store.list_related(b.entry_id)
    assert any(r["relation_type"] == "SUPERSEDES" and r["dst_id"] == a.entry_id for r in rels)

    # search is active-only → only the latest surfaces
    ids = {e.id for e in store.search("beta")}
    assert b.entry_id in ids
    assert {e.id for e in store.search("alpha")} == set()  # superseded, not findable


def test_supersede_false_keeps_both_related(store: ArchivistStore) -> None:
    a = store.publish(_entry("auth uses jwt alpha"))
    b = store.publish(_entry("auth uses jwt beta"), supersede=False)
    assert b.outcome == "related"
    assert store.get(a.entry_id).status == "active"
    assert store.get(b.entry_id).status == "active"
    rels = store.list_related(b.entry_id)
    assert any(r["relation_type"] == "RELATED" for r in rels)


def test_supersede_first_entry_is_new(store: ArchivistStore) -> None:
    r = store.publish(_entry("only one"), supersede=True)
    assert r.outcome == "new"  # nothing to supersede


def test_true_duplicate_not_resurrected_under_supersede(store: ArchivistStore) -> None:
    store.publish(_entry("identical content"))
    dup = store.publish(_entry("identical content"), supersede=True)
    assert dup.outcome in ("duplicate_l0", "duplicate_l1")


def test_supersede_chain_keeps_only_latest_active(store: ArchivistStore) -> None:
    a = store.publish(_entry("auth jwt alpha"))
    b = store.publish(_entry("auth jwt beta"), supersede=True)
    c = store.publish(_entry("auth jwt gamma"), supersede=True)
    assert store.get(a.entry_id).status == "superseded"
    assert store.get(b.entry_id).status == "superseded"
    assert store.get(c.entry_id).status == "active"
    assert {e.id for e in store.search("gamma")} == {c.entry_id}


def test_supersede_scoped_to_resource_and_type(store: ArchivistStore) -> None:
    a = store.publish(_entry("alpha", resource="svc-a"))
    # different resource → no supersession, just a new entry
    b = store.publish(_entry("beta", resource="svc-b"), supersede=True)
    assert b.outcome == "new"
    assert store.get(a.entry_id).status == "active"
    # same resource, different entry_type → also not superseded
    c = store.publish(_entry("alpha as a fact", resource="svc-a", etype="fact"), supersede=True)
    assert c.outcome == "new"
    assert store.get(a.entry_id).status == "active"
