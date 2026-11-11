"""Give entries written before cabal#410 their origin and lineage.

Entries published before origin existed have empty origin fields and no
lineage rows. This fills them, and changes nothing that is already set:

  - origin_archivist: the archivist name given, on every entry where it is empty.
  - origin_cabal: the cabal given, on entries where it is empty and one of
    whose refs starts with ``ref_prefix`` (or on all of them if no prefix).
  - origin_agent: the techne instance in a ``techne:<instance>:knowledge/<id>``
    ref, where it is empty.
  - lineage: one row per ref, so erasing a source reaches the entry.

Dry-run by default. Admin-only, run as ``cbl archivist backfill-origin``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cabal.archivist.purge import BUSY_TIMEOUT_MS
from cabal.archivist.schema import connect, init_db

logger = logging.getLogger(__name__)

_TECHNE_REF = re.compile(r"^techne:([^:]+):knowledge/")


@dataclass(slots=True)
class BackfillResult:
    applied: bool
    archivist: int = 0
    cabal: int = 0
    agent: int = 0
    lineage: int = 0


def backfill_origin(
    db_path: Path,
    *,
    archivist: str,
    cabal: str = "",
    ref_prefix: str = "",
    apply: bool = False,
) -> BackfillResult:
    if not db_path.is_file():
        raise FileNotFoundError(f"no archivist database at {db_path}")
    init_db(db_path).close()  # adds the cabal#410 columns and tables if missing
    conn = connect(db_path)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    result = BackfillResult(applied=apply)
    now = datetime.now(UTC).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, refs, origin_archivist, origin_cabal, origin_agent FROM memory_entries"
        ).fetchall()
        for r in rows:
            try:
                refs = [x for x in json.loads(r["refs"] or "[]") if isinstance(x, str)]
            except (json.JSONDecodeError, TypeError):
                refs = []
            sets: dict[str, str] = {}
            if archivist and not r["origin_archivist"]:
                sets["origin_archivist"] = archivist
                result.archivist += 1
            if cabal and not r["origin_cabal"] and (
                not ref_prefix or any(x.startswith(ref_prefix) for x in refs)
            ):
                sets["origin_cabal"] = cabal
                result.cabal += 1
            if not r["origin_agent"]:
                agents = [m.group(1) for x in refs if (m := _TECHNE_REF.match(x))]
                if agents:
                    sets["origin_agent"] = agents[0]
                    result.agent += 1
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                conn.execute(
                    f"UPDATE memory_entries SET {cols} WHERE id = ?", [*sets.values(), r["id"]]
                )
            for ref in refs:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO memory_lineage (derived_id, source_ref, created_at) "
                    "VALUES (?, ?, ?)",
                    (r["id"], ref, now),
                )
                result.lineage += cur.rowcount
        if apply:
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    logger.debug("archivist backfill-origin %s: %s", "apply" if apply else "dry-run", result)
    return result
