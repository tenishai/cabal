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


# ---- #26: body-only updates reach supersede (full-content hash) ----------


def test_supersede_on_body_only_change(store: ArchivistStore) -> None:
    """Same summary, changed details must supersede the prior — not be dropped
    as a duplicate (the L1 hash now covers the full content) (#26)."""
    a = store.publish(
        Entry(resource_name="r", summary="same headline", entry_type="decision", details="v1 body")
    )
    assert a.outcome == "new"
    b = store.publish(
        Entry(
            resource_name="r",
            summary="same headline",
            entry_type="decision",
            details="v2 body — materially changed",
        ),
        supersede=True,
    )
    assert b.outcome == "superseded"
    assert store.get(a.entry_id).status == "superseded"
    assert store.get(b.entry_id).status == "active"


def test_truly_identical_entry_still_dedups(store: ArchivistStore) -> None:
    """A genuinely identical re-publish (all content fields equal) is still a
    no-op duplicate — the full-content hash didn't break dedup."""
    base = dict(resource_name="r", summary="s", entry_type="fact", details="body", gotchas="g")
    store.publish(Entry(**base))
    dup = store.publish(Entry(**base), supersede=True)
    assert dup.outcome in ("duplicate_l0", "duplicate_l1")


# ---- #27: supersession retires ALL priors, atomically --------------------


def test_supersede_retires_more_than_five_priors(store: ArchivistStore) -> None:
    """Supersession retires EVERY active same-(resource,type) prior, not the
    dedup LIMIT-5 sample (#27)."""
    ids = [
        store.publish(_entry(f"variant {i}", resource="r", etype="fact"), supersede=False).entry_id
        for i in range(7)
    ]
    assert all(store.get(i).status == "active" for i in ids)  # all 7 active

    final = store.publish(_entry("the final word", resource="r", etype="fact"), supersede=True)
    assert final.outcome == "superseded"
    assert len(final.related_ids) == 7  # all 7, not capped at 5
    assert all(store.get(i).status == "superseded" for i in ids)
    assert store.get(final.entry_id).status == "active"
