"""A supersede that exactly matches an active entry still retires the others (#363).

Store-level only, no liminal import, so these run in the unit CI job (#359).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cabal.archivist import ArchivistStore, Entry
from cabal.archivist.schema import connect


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "archivist.db"


def _entry(summary: str, etype: str = "fact", resource: str = "r") -> Entry:
    return Entry(resource_name=resource, summary=summary, entry_type=etype)


def _status(store: ArchivistStore, entry_id: str) -> str:
    entry = store.get(entry_id)
    assert entry is not None
    return entry.status


@pytest.mark.parametrize("fresh_store", [False, True], ids=["l0", "l1"])
def test_resource_scope_duplicate_retires_other_entries(db: Path, fresh_store: bool) -> None:
    # The reproduction from the issue: the sink re-syncs an unchanged row while
    # a session has added another entry under the same resource.
    store = ArchivistStore(db)
    old = store.publish(_entry("old fact", "fact")).entry_id
    proc = store.publish(_entry("the procedure", "procedure")).entry_id
    other = store.publish(_entry("unrelated", "fact", resource="elsewhere")).entry_id

    if fresh_store:
        store = ArchivistStore(db)
    again = store.publish(
        _entry("the procedure", "procedure"), supersede=True, supersede_scope="resource"
    )

    assert again.outcome == ("duplicate_l1" if fresh_store else "duplicate_l0")
    assert again.entry_id == proc
    assert again.related_ids == [old]
    assert (_status(store, old), _status(store, proc), _status(store, other)) == (
        "superseded",
        "active",
        "active",
    )
    assert store.superseded_by(old) == [proc]
    assert [e.id for e in store.search("procedure")] == [proc]
    assert store.search("old") == []


def test_type_scope_duplicate_retires_only_the_same_type(db: Path) -> None:
    store = ArchivistStore(db)
    a = store.publish(_entry("mounted at a")).entry_id
    b = store.publish(_entry("mounted at b")).entry_id  # related: both stay active
    proc = store.publish(_entry("mount it at b", "procedure")).entry_id

    again = store.publish(_entry("mounted at b"), supersede=True)

    assert again.entry_id == b
    assert again.related_ids == [a]
    assert (_status(store, a), _status(store, b), _status(store, proc)) == (
        "superseded",
        "active",
        "active",
    )


def test_duplicate_without_supersede_retires_nothing(db: Path) -> None:
    store = ArchivistStore(db)
    a = store.publish(_entry("mounted at a")).entry_id
    store.publish(_entry("mounted at b"))

    again = store.publish(_entry("mounted at b"), supersede_scope="resource")

    assert again.related_ids == []
    assert _status(store, a) == "active"


@pytest.mark.parametrize("fresh_store", [False, True], ids=["same-process", "restarted"])
def test_republishing_retired_content_with_supersede_reasserts_it(
    db: Path, fresh_store: bool
) -> None:
    # Superseding with content an older entry held is a re-assertion: a new active
    # entry, and the one it replaces retired. It used to depend on the process. In
    # the one that retired the content, a stale L0 hit answered with the retired
    # id and changed nothing, so no active entry held the content (#353); after a
    # restart the same publish went through L1 and did this. Now both do this.
    store = ArchivistStore(db)
    old = store.publish(_entry("mounted at a")).entry_id
    new = store.publish(_entry("mounted at b"), supersede=True).entry_id
    assert _status(store, old) == "superseded"
    if fresh_store:
        store = ArchivistStore(db)

    again = store.publish(_entry("mounted at a"), supersede=True, supersede_scope="resource")

    assert again.outcome == "superseded"
    assert again.related_ids == [new]
    assert again.entry_id not in (old, new)
    assert (_status(store, old), _status(store, new)) == ("superseded", "superseded")
    assert _status(store, again.entry_id) == "active"


def test_duplicate_supersede_is_one_transaction(db: Path) -> None:
    store = ArchivistStore(db)
    a = store.publish(_entry("one", "fact")).entry_id
    b = store.publish(_entry("two", "decision")).entry_id
    store.publish(_entry("current", "procedure"))
    # Let the first SUPERSEDES edge in and refuse the second, so the failure
    # lands after one entry has already been retired.
    conn = connect(db)
    conn.execute(
        """CREATE TRIGGER fail_second_edge BEFORE INSERT ON memory_relations
           WHEN (SELECT COUNT(*) FROM memory_relations WHERE relation_type='SUPERSEDES') >= 1
           BEGIN SELECT RAISE(ABORT, 'injected'); END"""
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.Error, match="injected"):
        store.publish(_entry("current", "procedure"), supersede=True, supersede_scope="resource")

    assert (_status(store, a), _status(store, b)) == ("active", "active")


# ---- the L0 cache must not report a retired entry as the duplicate (#353) ----


@pytest.mark.parametrize("scope", ["resource", "type"])
def test_republishing_retired_content_is_not_an_l0_duplicate(db: Path, scope: str) -> None:
    store = ArchivistStore(db)
    x = store.publish(_entry("fact X"))
    replacement = _entry("fact Y", etype="procedure" if scope == "resource" else "fact")
    store.publish(replacement, supersede=True, supersede_scope=scope)
    assert _status(store, x.entry_id) == "superseded"

    again = store.publish(_entry("fact X"))  # same process: X's hash is still in L0

    assert again.outcome not in ("duplicate_l0", "duplicate_l1")
    assert _status(store, again.entry_id) == "active"
    assert again.entry_id != x.entry_id
