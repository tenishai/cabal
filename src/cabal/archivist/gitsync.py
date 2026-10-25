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

export_corpus(store, dir)         SQLite -> files (entries + markers).
materialize_corpus(dir, db_path)  files -> a fresh local SQLite (status derived).
"""

from __future__ import annotations

import json
from pathlib import Path

from cabal.archivist.models import Entry, Relation
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
        "source_thread": row["source_thread"],
        "created_at": row["created_at"],
        "content_hash": row["content_hash"],
    }


def export_corpus(store: ArchivistStore, out_dir: Path) -> dict:
    """SQLite -> git files. Writes `entries/<hash>.json` (every status) and
    `supersedes/<by>__<target>.json` markers (from SUPERSEDES relations).
    Idempotent: content-addressed filenames overwrite identically. Returns counts."""
    out_dir = Path(out_dir)
    entries_dir = out_dir / "entries"
    supersedes_dir = out_dir / "supersedes"
    entries_dir.mkdir(parents=True, exist_ok=True)
    supersedes_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(store.db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM memory_entries").fetchall()]
        id_to_hash = {r["id"]: r["content_hash"] for r in rows}
        for r in rows:
            doc = _entry_to_doc(r)
            (entries_dir / f"{r['content_hash']}.json").write_text(
                json.dumps(doc, indent=2, sort_keys=True) + "\n"
            )

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
    return {"entries": len(rows), "markers": markers}


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

    present: set[str] = set()
    edir = corpus_dir / "entries"
    if edir.exists():
        for f in sorted(edir.glob("*.json")):
            d = json.loads(f.read_text())
            h = d.get("content_hash") or f.stem
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
