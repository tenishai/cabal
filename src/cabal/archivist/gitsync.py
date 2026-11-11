"""Git-syncable representation of the Archivist corpus (agent-model §5b/§5e, Phase 2).

The local Archivist is SQLite — a fast working store. To SHARE learnings across
machines/people via a git repo, the corpus is mirrored as plain text files that
merge conflict-free:

  entries/<content_hash>.json     immutable, content-addressed entry. The filename
                                  IS the identity, and the file never stores
                                  `status` — so two machines that distil the same
                                  lesson produce the *same* file (idempotent).
  supersedes/<by>__<target>.json  append-only marker: entry <by> retires entry
                                  <target> (both by content hash). One file per
                                  edge → pure adds, no merge conflicts.

`status` (active|superseded) is DERIVED on materialization: an entry is superseded
iff some marker targets its content hash, unless it superseded something itself
LATER than that (by the markers' `at`): re-publishing retired content with
supersede=True re-asserts it, and since identity is the content hash both rows
share one file (#367). So supersession survives a git merge of three
contributors without mutating any entry file — the CRDT-ish property that makes
a git hub work.

Liminal-free: SQLite + json + files only.

export_corpus(store, dir)         SQLite -> files (entries + markers). An erased
                                  entry is written as erasures/<hash>.json, never
                                  as an entry file (cabal#410).
materialize_corpus(dir, db_path)  files -> a fresh local SQLite (status derived).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cabal.archivist.models import Entry, Relation, text_hash
from cabal.archivist.schema import connect
from cabal.archivist.store import ArchivistStore


def _entry_to_doc(row: dict) -> dict:
    """Content + provenance only. `id`/`status`/`updated_at`/`access_count`/
    `processing_state` are excluded — id+status are derived, the rest is local
    runtime state."""
    return {
        "resource_name": row["resource_name"],
        "entry_type": row["entry_type"],
        "summary": row["summary"],
        "details": row["details"],
        "gotchas": row["gotchas"],
        "commands": json.loads(row["commands"] or "[]"),
        "refs": json.loads(row["refs"] or "[]"),
        "tags": json.loads(row["tags"] or "[]"),
        "importance": row["importance"],
        "written_by": row["written_by"],
        "origin_house": row.get("origin_house") or "",
        "origin_cabal": row.get("origin_cabal") or "",
        "origin_agent": row.get("origin_agent") or "",
        "origin_role": row.get("origin_role") or "",
        "access": row.get("access") or "house",
        "source_thread": row["source_thread"],
        "created_at": row["created_at"],
        "content_hash": row["content_hash"],
    }


def _read_erasure_markers(corpus_dir: Path) -> tuple[set[str], set[str], list[tuple[str, str]]]:
    """The erasure markers under ``corpus_dir/erasures``: the content hashes and
    text hashes they cover, and every (ref, kind) to record in memory_erasures,
    hashes, erased entry ids and erased sources alike (cabal#410)."""
    content: set[str] = set()
    text: set[str] = set()
    marks: list[tuple[str, str]] = []
    xdir = Path(corpus_dir) / "erasures"
    if not xdir.exists():
        return content, text, marks
    for f in sorted(xdir.glob("*.json")):
        m = json.loads(f.read_text())
        h = m.get("content_hash") or f.stem
        content.add(h)
        marks.append((h, "content"))
        for t in m.get("text_hashes") or []:
            if isinstance(t, str):
                text.add(t)
                marks.append((t, "text"))
        marks.extend((i, "entry") for i in m.get("entry_ids") or [] if isinstance(i, str))
        marks.extend((r, "source") for r in m.get("sources") or [] if isinstance(r, str))
    return content, text, marks


def _covered(row: dict, content: set[str], text: set[str]) -> bool:
    return row["content_hash"] in content or text_hash(
        row.get("summary"), row.get("details"), row.get("gotchas")
    ) in text


def prune_erased_files(corpus_dir: Path) -> int:
    """Remove every ``entries/*.json`` an erasure marker covers, by content hash
    or text hash. Returns how many were removed. Run after a merge, so an entry
    file a teammate re-added before the marker arrived does not survive."""
    content, text, _ = _read_erasure_markers(corpus_dir)
    if not content and not text:
        return 0
    removed = 0
    for f in sorted((Path(corpus_dir) / "entries").glob("*.json")):
        d = json.loads(f.read_text())
        if _covered({"content_hash": d.get("content_hash") or f.stem, **d}, content, text):
            f.unlink()
            removed += 1
    return removed


def export_corpus(store: ArchivistStore, out_dir: Path) -> dict:
    """SQLite -> git files. Writes `entries/<hash>.json` (every status) and
    `supersedes/<by>__<target>.json` markers (from SUPERSEDES relations).
    Idempotent: content-addressed filenames overwrite identically. Returns counts.

    Erasure markers already in ``out_dir`` win over the local store (cabal#410):
    a teammate whose store predates an erase pulled in from git must not write
    the erased text back, so an entry a marker covers, by content or text hash,
    is skipped and its file removed."""
    out_dir = Path(out_dir)
    entries_dir = out_dir / "entries"
    supersedes_dir = out_dir / "supersedes"
    erasures_dir = out_dir / "erasures"
    entries_dir.mkdir(parents=True, exist_ok=True)
    supersedes_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(store.db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM memory_entries").fetchall()]
        id_to_hash = {r["id"]: r["content_hash"] for r in rows}
        known_content, known_text, _ = _read_erasure_markers(out_dir)
        erased = 0
        skipped = 0
        for r in rows:
            entry_file = entries_dir / f"{r['content_hash']}.json"
            if r["status"] != "erased" and _covered(r, known_content, known_text):
                entry_file.unlink(missing_ok=True)
                skipped += 1
                continue
            if r["status"] == "erased":
                # An erased entry leaves the corpus and a marker takes its place,
                # so a later materialize cannot bring the text back (cabal#410).
                entry_file.unlink(missing_ok=True)
                erasures_dir.mkdir(parents=True, exist_ok=True)
                text_hashes = [
                    x["ref"]
                    for x in conn.execute(
                        "SELECT ref FROM memory_erasures WHERE kind = 'text' AND entry_id = ? "
                        "ORDER BY ref",
                        (r["id"],),
                    ).fetchall()
                ]
                # Identifiers, never text: the erased sources this entry came
                # from, so a replica refuses a publish derived from them.
                sources = [
                    x["source_ref"]
                    for x in conn.execute(
                        "SELECT l.source_ref FROM memory_lineage l "
                        "JOIN memory_erasures x ON x.ref = l.source_ref "
                        "WHERE l.derived_id = ? ORDER BY l.source_ref",
                        (r["id"],),
                    ).fetchall()
                ]
                (erasures_dir / f"{r['content_hash']}.json").write_text(
                    json.dumps(
                        {
                            "content_hash": r["content_hash"],
                            "text_hashes": text_hashes,
                            "entry_ids": sorted({r["id"], r["content_hash"]}),
                            "sources": sources,
                            "at": r["updated_at"],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
                erased += 1
                continue
            doc = _entry_to_doc(r)
            entry_file.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")

        markers = 0
        rels = conn.execute(
            "SELECT src_id, dst_id, created_at FROM memory_relations "
            "WHERE relation_type = 'SUPERSEDES'"
        ).fetchall()
        for rel in rels:
            by = id_to_hash.get(rel["src_id"])
            target = id_to_hash.get(rel["dst_id"])
            if not by or not target:
                continue
            marker = {"by": by, "supersedes": target, "at": rel["created_at"]}
            (supersedes_dir / f"{by}__{target}.json").write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n"
            )
            markers += 1
    finally:
        conn.close()
    return {
        "entries": len(rows) - erased - skipped,
        "markers": markers,
        "erased": erased,
        "skipped_erased": skipped,
    }


def materialize_corpus(corpus_dir: Path, db_path: Path) -> ArchivistStore:
    """git files -> a fresh local SQLite. Identity = content hash; `status` is
    DERIVED (superseded iff a marker targets the entry's hash). The db is rebuilt
    from scratch each call — it's a materialized view of the files."""
    corpus_dir = Path(corpus_dir)
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()  # rebuild the view from scratch
    store = ArchivistStore(db_path)

    edges: list[tuple[str, str, str | None]] = []
    sdir = corpus_dir / "supersedes"
    if sdir.exists():
        for f in sorted(sdir.glob("*.json")):
            m = json.loads(f.read_text())
            edges.append((m["by"], m["supersedes"], m.get("at")))
    superseded = _superseded_hashes(edges)

    erased, erased_text, marks = _read_erasure_markers(corpus_dir)
    if marks:
        now = datetime.now(UTC).isoformat()
        conn = connect(db_path)
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO memory_erasures (ref, kind, reason, erased_at) "
                "VALUES (?, ?, 'git corpus erasure marker', ?)",
                [(ref, kind, now) for ref, kind in marks],
            )
            conn.commit()
        finally:
            conn.close()

    present: set[str] = set()
    edir = corpus_dir / "entries"
    if edir.exists():
        for f in sorted(edir.glob("*.json")):
            d = json.loads(f.read_text())
            h = d.get("content_hash") or f.stem
            if _covered({"content_hash": h, **d}, erased, erased_text):
                continue  # an erasure marker wins over a stale entry file
            present.add(h)
            store._insert(
                Entry(
                    id=h,
                    content_hash=h,
                    resource_name=d["resource_name"],
                    entry_type=d["entry_type"],
                    summary=d["summary"],
                    details=d.get("details"),
                    gotchas=d.get("gotchas"),
                    commands=d.get("commands", []),
                    refs=d.get("refs", []),
                    tags=d.get("tags", []),
                    importance=d.get("importance", 1.0),
                    written_by=d.get("written_by", "unknown"),
                    origin_house=d.get("origin_house", ""),
                    origin_cabal=d.get("origin_cabal", ""),
                    origin_agent=d.get("origin_agent", ""),
                    origin_role=d.get("origin_role", ""),
                    access=d.get("access", "house"),
                    source_thread=d.get("source_thread"),
                    created_at=d.get("created_at"),
                    status="superseded" if h in superseded else "active",
                )
            )

    store._insert_relations(
        [
            Relation(
                src_id=by,
                dst_id=target,
                relation_type="SUPERSEDES",
                metadata={"reason": "same_resource_supersedes"},
                created_at=at,
            )
            for (by, target, at) in edges
            if by in present and target in present
        ]
    )
    return store


def _superseded_hashes(edges: list[tuple[str, str, str | None]]) -> set[str]:
    """Hashes whose latest marker retires them (#367).

    A hash a marker targets is superseded, unless the same hash also appears as
    `by` in a marker with a later `at`: it was re-asserted after it was retired.
    Without this, re-asserting A after B superseded it left markers both ways
    and both entries superseded, so the resource had no active entry. A marker
    with no `at` (older exports) counts as earliest, so a hash that is only ever
    targeted stays superseded, as before."""
    last_targeted: dict[str, str] = {}
    last_by: dict[str, str] = {}
    for by, target, at in edges:
        stamp = at or ""
        if stamp >= last_targeted.get(target, ""):
            last_targeted[target] = stamp
        if stamp >= last_by.get(by, ""):
            last_by[by] = stamp
    return {
        h for h, t in last_targeted.items() if h not in last_by or t >= last_by[h]
    }
