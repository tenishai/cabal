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
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize a fresh Archivist database. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(ARCHIVIST_SCHEMA_SQL)
    conn.commit()
    return conn


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection. Caller closes."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
