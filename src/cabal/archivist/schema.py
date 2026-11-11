"""Archivist SQLite schema + init.

Owns its own DB because the entry schema is too richly typed to live
inside techne.knowledge (gotchas, commands, refs as dedicated fields
rather than tags). Search still uses FTS5; cross-references go through
the memory_relations table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ARCHIVIST_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_entries (
    id              TEXT PRIMARY KEY,
    resource_name   TEXT NOT NULL,
    summary         TEXT NOT NULL,
    entry_type      TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',
    details         TEXT,
    gotchas         TEXT,
    commands        TEXT NOT NULL DEFAULT '[]',
    refs            TEXT NOT NULL DEFAULT '[]',
    importance      REAL NOT NULL DEFAULT 1.0,
    source_thread   TEXT,
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    processing_state TEXT NOT NULL DEFAULT 'indexed',
    written_by      TEXT NOT NULL,
    origin_archivist    TEXT NOT NULL DEFAULT '',
    origin_cabal    TEXT NOT NULL DEFAULT '',
    origin_agent    TEXT NOT NULL DEFAULT '',
    origin_role     TEXT NOT NULL DEFAULT '',
    access          TEXT NOT NULL DEFAULT 'internal',
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_resource
    ON memory_entries(resource_name, entry_type, status);
CREATE INDEX IF NOT EXISTS idx_memory_entries_hash
    ON memory_entries(content_hash, status);
CREATE INDEX IF NOT EXISTS idx_memory_entries_type
    ON memory_entries(entry_type, status);
CREATE INDEX IF NOT EXISTS idx_memory_entries_status
    ON memory_entries(status);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts USING fts5(
    resource_name,
    summary,
    details,
    gotchas,
    tags,
    content=''
);

CREATE TABLE IF NOT EXISTS memory_relations (
    src_id          TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    relation_type   TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    PRIMARY KEY (src_id, dst_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_relations_src
    ON memory_relations(src_id);
CREATE INDEX IF NOT EXISTS idx_memory_relations_dst
    ON memory_relations(dst_id);

-- Lineage (cabal#410): which sources an entry was derived from. A source is
-- another entry's id or an outside reference such as
-- "techne:poe@dimartin:knowledge/4255". Erasing a source walks this table.
CREATE TABLE IF NOT EXISTS memory_lineage (
    derived_id      TEXT NOT NULL,
    source_ref      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (derived_id, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_memory_lineage_source
    ON memory_lineage(source_ref);

-- Erasures (cabal#410): every reference and content hash that was erased, so
-- a later publish or a git-corpus sync cannot bring the text back.
CREATE TABLE IF NOT EXISTS memory_erasures (
    ref             TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    entry_id        TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    erased_at       TEXT NOT NULL
);
"""

# Columns added after the first release, with the DDL that adds each one to a
# database created before it. CREATE TABLE IF NOT EXISTS leaves an existing
# table alone, so init_db adds whatever is missing.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("origin_archivist", "TEXT NOT NULL DEFAULT ''"),
    ("origin_cabal", "TEXT NOT NULL DEFAULT ''"),
    ("origin_agent", "TEXT NOT NULL DEFAULT ''"),
    ("origin_role", "TEXT NOT NULL DEFAULT ''"),
    ("access", "TEXT NOT NULL DEFAULT 'internal'"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(memory_entries)")}
    for name, ddl in _ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE memory_entries ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_entries_origin "
        "ON memory_entries(origin_cabal, origin_role)"
    )


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize a fresh Archivist database. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(ARCHIVIST_SCHEMA_SQL)
    _add_missing_columns(conn)
    conn.commit()
    return conn


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection. Caller closes."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
