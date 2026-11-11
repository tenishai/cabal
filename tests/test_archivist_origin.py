"""cabal#410: origin on every entry, ranking by origin, lineage and erasure.

Store-level and CLI, no liminal import, so these run in unit CI. The handler
surface is in test_archivist_origin_handler.py.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from cabal.archivist import ArchivistStore, Entry
from cabal.archivist.backfill import backfill_origin
from cabal.archivist.erase import erase
from cabal.archivist.gitsync import export_corpus, materialize_corpus, prune_erased_files
from cabal.archivist.schema import ARCHIVIST_SCHEMA_SQL, init_db
from cabal.cli.main import app

runner = CliRunner()
SECRET = "zqxwithdrawnvalue"


def _rows(db: Path, sql: str, *args) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _file_bytes(db: Path) -> bytes:
    data = db.read_bytes()
    wal = db.with_name(db.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


# ---- schema upgrade ------------------------------------------------------


def test_init_db_adds_origin_columns_to_an_old_database(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    old_sql = ARCHIVIST_SCHEMA_SQL
    for col in ("origin_archivist", "origin_cabal", "origin_agent", "origin_role"):
        old_sql = old_sql.replace(f"    {col}    TEXT NOT NULL DEFAULT '',\n", "")
    old_sql = old_sql.replace("    access          TEXT NOT NULL DEFAULT 'internal',\n", "")
    assert "origin_cabal" not in old_sql.split("memory_lineage")[0]
    conn = sqlite3.connect(db)
    conn.executescript(old_sql)
    conn.execute(
        "INSERT INTO memory_entries (id, resource_name, summary, entry_type, content_hash, "
        "written_by, created_at, updated_at) VALUES ('e1','r','s','fact','h','w','t','t')"
    )
    conn.commit()
    conn.close()

    init_db(db).close()
    init_db(db).close()  # idempotent

    cols = {r[1] for r in _rows(db, "PRAGMA table_info(memory_entries)")}
    assert {"origin_archivist", "origin_cabal", "origin_agent", "origin_role", "access"} <= cols
    assert _rows(db, "SELECT access, origin_cabal FROM memory_entries") == [("internal", "")]


# ---- origin and ranking --------------------------------------------------


def _entry(name: str, summary: str, cabal: str = "", role: str = "") -> Entry:
    return Entry(
        resource_name=name,
        summary=summary,
        entry_type="fact",
        origin_archivist="mash1",
        origin_cabal=cabal,
        origin_role=role,
    )


def test_origin_round_trips(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "a.db")
    r = store.publish(_entry("x", "nul rule for store writes", cabal="techne-next", role="reviewer"))
    got = store.get(r.entry_id)
    assert (got.origin_archivist, got.origin_cabal, got.origin_role, got.access) == (
        "mash1",
        "techne-next",
        "reviewer",
        "internal",
    )


def test_search_ranks_own_cabal_then_same_role_then_rest(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "a.db")
    # The "rest" entry matches the query best by bm25 (the word three times),
    # so only the origin ordering can put it last.
    rest = store.publish(_entry("c", "flaky flaky flaky test", cabal="other", role="coder"))
    role = store.publish(_entry("b", "flaky test seen", cabal="other", role="reviewer"))
    own = store.publish(_entry("a", "a flaky test in this cabal's suite", cabal="mine", role="coder"))

    plain = [e.id for e in store.search("flaky")]
    assert plain[0] == rest.entry_id  # bm25 alone

    ranked = [
        e.id for e in store.search("flaky", caller_cabal="mine", caller_role="reviewer")
    ]
    assert ranked == [own.entry_id, role.entry_id, rest.entry_id]


def test_ranking_hides_nothing(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "a.db")
    for i, cabal in enumerate(["a", "b", "c"]):
        store.publish(_entry(f"r{i}", f"shared lesson {cabal}", cabal=cabal))
    assert len(store.search("shared lesson", caller_cabal="a")) == 3


def test_empty_caller_role_does_not_match_entries_without_a_role(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "a.db")
    # Without the "? <> ''" guard an empty caller role equals every empty
    # origin_role, lifting the no-role entry into the same-role group ahead of
    # the role-bearing one, which has the better bm25 score.
    norole = store.publish(_entry("a", "lesson in a long sentence about other things", cabal="x"))
    withrole = store.publish(_entry("c", "lesson lesson lesson", cabal="y", role="coder"))
    mine = store.publish(_entry("b", "lesson", cabal="mine"))
    ranked = [e.id for e in store.search("lesson", caller_cabal="mine")]
    assert ranked == [mine.entry_id, withrole.entry_id, norole.entry_id]


def test_empty_caller_cabal_does_not_match_entries_without_a_cabal(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "a.db")
    nocabal = store.publish(_entry("a", "lesson in a long sentence about other things"))
    withcabal = store.publish(_entry("c", "lesson lesson lesson", cabal="y", role="other"))
    mine = store.publish(_entry("b", "lesson", cabal="z", role="reviewer"))
    ranked = [e.id for e in store.search("lesson", caller_role="reviewer")]
    assert ranked == [mine.entry_id, withcabal.entry_id, nocabal.entry_id]


# ---- lineage -------------------------------------------------------------


def test_publish_records_refs_and_derived_from_as_lineage(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "a.db")
    src = store.publish(_entry("s", "a source"))
    e = Entry(
        resource_name="m",
        summary="merged",
        entry_type="fact",
        refs=["techne:poe@dimartin:knowledge/1"],
    )
    r = store.publish(e, derived_from=[src.entry_id, src.entry_id])
    assert store.lineage(r.entry_id) == sorted([src.entry_id, "techne:poe@dimartin:knowledge/1"])


def test_duplicate_publish_with_a_new_source_joins_lineage(tmp_path: Path) -> None:
    store = ArchivistStore(tmp_path / "a.db")
    first = store.publish(_entry("d", "same text"), derived_from=["ref:a"])
    again = store.publish(_entry("d", "same text"), derived_from=["ref:b"])
    assert again.outcome == "duplicate_l0"
    assert store.lineage(first.entry_id) == ["ref:a", "ref:b"]


# ---- erasure -------------------------------------------------------------


def _seed_chain(store: ArchivistStore) -> tuple[str, str, str, str]:
    """source ref -> a -> merged (also from b) -> top. Plus an unrelated entry."""
    a = store.publish(
        Entry(resource_name="a", summary=f"a holds {SECRET}", entry_type="fact", refs=["src:1"])
    ).entry_id
    b = store.publish(Entry(resource_name="b", summary="b is fine", entry_type="fact")).entry_id
    merged = store.publish(
        Entry(resource_name="m", summary=f"merged {SECRET}", entry_type="fact"),
        derived_from=[a, b],
    ).entry_id
    top = store.publish(
        Entry(resource_name="t", summary="top tier copy", entry_type="fact"),
        derived_from=[merged],
    ).entry_id
    store.publish(Entry(resource_name="keep", summary="unrelated", entry_type="fact"))
    return a, b, merged, top


def test_erase_dry_run_changes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    a, _b, merged, top = _seed_chain(store)
    result = erase(db, "src:1")
    assert not result.applied
    assert {e.id for e in result.entries} == {a, merged, top}
    assert _rows(db, "SELECT count(*) FROM memory_entries WHERE status='erased'") == [(0,)]
    assert _rows(db, "SELECT count(*) FROM memory_erasures") == [(0,)]


def test_erase_walks_lineage_and_reports_other_sources(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    a, b, merged, top = _seed_chain(store)
    result = erase(db, "src:1", reason="withdrawn", apply=True)

    by_id = {e.id: e for e in result.entries}
    assert set(by_id) == {a, merged, top}
    assert by_id[merged].other_sources == [b]  # left for the writer to re-derive from
    assert by_id[merged].via == a

    statuses = dict(_rows(db, "SELECT id, status FROM memory_entries"))
    assert statuses[a] == statuses[merged] == statuses[top] == "erased"
    assert statuses[b] == "active"
    assert store.search("unrelated")  # the rest is untouched
    assert store.search(SECRET) == []
    assert store.search("merged") == []

    kept = _rows(db, "SELECT summary, details, tags, refs FROM memory_entries WHERE id=?", a)
    assert kept == [("", None, "[]", "[]")]
    assert SECRET.encode() not in _file_bytes(db)


def test_erased_content_or_source_is_refused_on_publish(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    _seed_chain(store)
    erase(db, "src:1", apply=True)
    store = ArchivistStore(db)  # fresh L0 cache, as after a restart

    same = store.publish(
        Entry(resource_name="a", summary=f"a holds {SECRET}", entry_type="fact", refs=["src:1"])
    )
    assert same.outcome == "refused_erased"
    assert same.entry_id == ""

    via_ref = store.publish(
        Entry(resource_name="new", summary="new text", entry_type="fact", refs=["src:1"])
    )
    assert via_ref.outcome == "refused_erased"
    assert via_ref.related_ids == ["src:1"]

    via_hash_only = store.publish(
        Entry(resource_name="a", summary=f"a holds {SECRET}", entry_type="fact")
    )
    # Same content, no refs: still refused, by content hash.
    assert via_hash_only.outcome == "refused_erased"

    ok = store.publish(Entry(resource_name="fresh", summary="fresh", entry_type="fact"))
    assert ok.outcome == "new"


def test_erase_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    _seed_chain(store)
    erase(db, "src:1", apply=True)
    again = erase(db, "src:1", apply=True)
    assert {e.status for e in again.entries} == {"erased"}
    assert _rows(db, "INSERT INTO memory_entries_fts(memory_entries_fts) VALUES ('integrity-check')") == []


def test_erase_lists_unreached_successors_and_keeps_them(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    old = store.publish(
        Entry(resource_name="f", summary="old wording", entry_type="fact", refs=["src:old"])
    ).entry_id
    new = store.publish(
        Entry(resource_name="f", summary="corrected wording", entry_type="fact", refs=["src:new"]),
        supersede=True,
    ).entry_id
    result = erase(db, "src:old", apply=True)
    assert [(e.id, e.successors) for e in result.entries] == [(old, [new])]
    assert dict(_rows(db, "SELECT id, status FROM memory_entries"))[new] == "active"


def test_erase_by_entry_id(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    a, b, merged, top = _seed_chain(store)
    result = erase(db, b, apply=True)
    assert {e.id for e in result.entries} == {b, merged, top}
    assert dict(_rows(db, "SELECT id, status FROM memory_entries"))[a] == "active"


def test_git_sync_cannot_bring_an_erased_entry_back(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    a, _b, merged, top = _seed_chain(store)
    corpus = tmp_path / "corpus"
    export_corpus(store, corpus)  # before the erase: entry files hold the text
    stale = {f.name for f in (corpus / "entries").glob("*.json")}

    erase(db, "src:1", apply=True)
    counts = export_corpus(ArchivistStore(db), corpus)
    assert counts["erased"] == 3
    left = {f.name for f in (corpus / "entries").glob("*.json")}
    assert len(left) == len(stale) - 3
    for f in (corpus / "entries").glob("*.json"):
        assert SECRET not in f.read_text()

    # A clone that pulled the erasure markers but still has a stale entry file
    # holding the erased text: the marker wins.
    other = tmp_path / "other"
    export_corpus(ArchivistStore(db), other)
    for f in (corpus / "erasures").glob("*.json"):
        h = json.loads(f.read_text())["content_hash"]
        (other / "entries" / f"{h}.json").write_text(
            json.dumps(
                {"content_hash": h, "resource_name": "a", "entry_type": "fact",
                 "summary": f"a holds {SECRET}"}
            )
        )
    # And one under another name and hash, with the same words.
    (other / "entries" / "f00.json").write_text(
        json.dumps({"content_hash": "f00", "resource_name": "renamed", "entry_type": "fact",
                    "summary": f"a  holds {SECRET}", "refs": ["elsewhere"]})
    )
    view = materialize_corpus(other, tmp_path / "view.db")
    assert view.search(SECRET) == []
    # Each marker names its erased entry, and the clone records that id. The
    # kind is whichever mark reached the ref first (an entry id can also be a
    # lineage source); the publish refusal matches on ref alone.
    marker_ids = set()
    for f in (corpus / "erasures").glob("*.json"):
        m = json.loads(f.read_text())
        assert m["content_hash"] in m["entry_ids"]
        marker_ids.update(m["entry_ids"])
    assert {a, merged, top} <= marker_ids
    recorded = {
        r for (r,) in _rows(tmp_path / "view.db",
                            "SELECT ref FROM memory_erasures")
    }
    assert marker_ids <= recorded
    again = view.publish(Entry(resource_name="a", summary=f"a holds {SECRET}", entry_type="fact"))
    assert again.outcome == "refused_erased"


# ---- backfill --------------------------------------------------------------


def test_backfill_origin_fills_only_empty_fields(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    old = store.publish(
        Entry(
            resource_name="poe-dimartin-k1",
            summary="old",
            entry_type="fact",
            refs=["techne:poe@dimartin:knowledge/1"],
        )
    ).entry_id
    other = store.publish(Entry(resource_name="host-aware", summary="hand", entry_type="fact")).entry_id
    set_ = store.publish(_entry("s", "already set", cabal="techne-next")).entry_id
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM memory_lineage")  # as before #410
    conn.commit()
    conn.close()

    dry = backfill_origin(db, archivist="mash1", cabal="dev04", ref_prefix="techne:poe@dimartin:")
    assert (dry.archivist, dry.cabal, dry.agent, dry.lineage) == (2, 1, 1, 1)
    assert _rows(db, "SELECT count(*) FROM memory_lineage") == [(0,)]

    backfill_origin(db, archivist="mash1", cabal="dev04", ref_prefix="techne:poe@dimartin:", apply=True)
    rows = {r[0]: r[1:] for r in _rows(db, "SELECT id, origin_archivist, origin_cabal, origin_agent FROM memory_entries")}
    assert rows[old] == ("mash1", "dev04", "poe@dimartin")
    assert rows[other] == ("mash1", "", "")
    assert rows[set_] == ("mash1", "techne-next", "")
    assert store.lineage(old) == ["techne:poe@dimartin:knowledge/1"]

    again = backfill_origin(db, archivist="mash1", cabal="dev04", apply=True)
    assert (again.archivist, again.agent, again.lineage) == (0, 0, 0)


# ---- CLI -------------------------------------------------------------------


def test_cli_erase_and_backfill(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    _seed_chain(store)
    dry = runner.invoke(app, ["archivist", "erase", "src:1", "--db", str(db)])
    assert dry.exit_code == 0, dry.output
    assert "would erase 3 entries" in dry.output
    assert "re-run with --apply" in dry.output
    done = runner.invoke(app, ["archivist", "erase", "src:1", "--db", str(db), "--apply"])
    assert done.exit_code == 0, done.output
    assert "erased 3 entries" in done.output

    bf = runner.invoke(app, ["archivist", "backfill-origin", "--archivist", "mash1", "--db", str(db)])
    assert bf.exit_code == 0, bf.output
    assert "would set: archivist on 5" in bf.output

    missing = runner.invoke(app, ["archivist", "erase", "x", "--db", str(tmp_path / "none.db")])
    assert missing.exit_code == 2


def test_replica_refuses_a_publish_from_an_erased_source(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    _seed_chain(store)
    erase(db, "src:1", apply=True)
    corpus = tmp_path / "corpus"
    export_corpus(ArchivistStore(db), corpus)
    view = materialize_corpus(corpus, tmp_path / "view.db")
    reworded = view.publish(
        Entry(resource_name="r", summary="reworded entirely", entry_type="fact", refs=["src:1"])
    )
    assert reworded.outcome == "refused_erased"
    assert reworded.related_ids == ["src:1"]


def test_export_from_a_stale_store_does_not_rewrite_an_erased_entry(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store = ArchivistStore(db)
    _seed_chain(store)
    corpus = tmp_path / "corpus"
    export_corpus(store, corpus)
    stale_db = tmp_path / "stale.db"
    stale = materialize_corpus(corpus, stale_db)  # a teammate, before the erase

    erase(db, "src:1", apply=True)
    export_corpus(ArchivistStore(db), corpus)  # markers land in the shared tree
    counts = export_corpus(stale, corpus)  # the teammate exports its stale store
    assert counts["skipped_erased"] == 3
    for f in (corpus / "entries").glob("*.json"):
        assert SECRET not in f.read_text()


def test_stale_teammate_push_through_a_hub_cannot_bring_erased_text_back(tmp_path: Path) -> None:
    import subprocess

    from cabal.archivist import gitmemory
    from tests.test_archivist_gitmemory import _init_remote

    hub = _init_remote(tmp_path)
    a_dir, t_dir = tmp_path / "a", tmp_path / "t"
    gitmemory.ensure_clone(str(hub), a_dir)
    a_db = tmp_path / "a.db"
    a = gitmemory.pull(a_dir, a_db)
    a.publish(Entry(resource_name="x", summary=f"x holds {SECRET}", entry_type="fact", refs=["src:x"]))
    gitmemory.push(a, a_dir, message="seed")

    gitmemory.ensure_clone(str(hub), t_dir)
    t = gitmemory.pull(t_dir, tmp_path / "t.db")  # the teammate holds the entry

    erase(a_db, "src:x", apply=True)
    gitmemory.push(ArchivistStore(a_db), a_dir, message="erase")

    for i in range(2):  # the teammate keeps working without re-materialising
        t.publish(Entry(resource_name=f"n{i}", summary=f"new fact {i}", entry_type="fact"))
        gitmemory.push(t, t_dir, message=f"t{i}")

    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(hub), str(fresh)], check=True)
    for f in (fresh / "entries").glob("*.json"):
        assert SECRET not in f.read_text()
    assert list((fresh / "erasures").glob("*.json"))


def test_push_drops_an_entry_file_its_own_commit_added_before_the_marker_arrived(
    tmp_path: Path,
) -> None:
    import subprocess

    from cabal.archivist import gitmemory
    from tests.test_archivist_gitmemory import _init_remote

    hub = _init_remote(tmp_path)
    a_dir, t_dir = tmp_path / "a", tmp_path / "t"
    gitmemory.ensure_clone(str(hub), a_dir)
    gitmemory.ensure_clone(str(hub), t_dir)
    a_db = tmp_path / "a.db"
    a = gitmemory.pull(a_dir, a_db)
    t = gitmemory.pull(t_dir, tmp_path / "t.db")
    words = f"y holds {SECRET}"
    a.publish(Entry(resource_name="y", summary=words, entry_type="fact", refs=["src:y"]))
    gitmemory.push(a, a_dir, message="seed")
    erase(a_db, "src:y", apply=True)
    gitmemory.push(ArchivistStore(a_db), a_dir, message="erase")

    # The teammate never pulled, and writes the same words on its own. Its
    # export runs before the merge that brings the marker in.
    t.publish(Entry(resource_name="y2", summary=words, entry_type="fact"))
    gitmemory.push(t, t_dir, message="t")

    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(hub), str(fresh)], check=True)
    for f in (fresh / "entries").glob("*.json"):
        assert SECRET not in f.read_text()


def test_marker_covers_an_entry_file_with_a_null_content_hash(tmp_path: Path) -> None:
    # An entry file whose content_hash is null falls back to its file name, so a
    # marker for that name still covers it, in materialize and in prune.
    corpus = tmp_path / "corpus"
    (corpus / "entries").mkdir(parents=True)
    (corpus / "erasures").mkdir()
    (corpus / "erasures" / "deadbeef.json").write_text(
        json.dumps({"content_hash": "deadbeef", "text_hashes": [], "entry_ids": []})
    )
    entry = corpus / "entries" / "deadbeef.json"
    entry.write_text(
        json.dumps({"content_hash": None, "resource_name": "x", "entry_type": "fact",
                    "summary": "text no marker hashes"})
    )
    view = materialize_corpus(corpus, tmp_path / "view.db")
    assert view.search("hashes") == []
    assert prune_erased_files(corpus) == 1
    assert not entry.exists()
