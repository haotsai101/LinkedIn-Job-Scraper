"""Migration 001 — secondary indexes + WAL journal mode.

Additive only: creates four indexes on the ``jobs`` table that back the
apply-agent's hot pending-jobs query (``apply_jobs.get_pending_jobs``) and
switches the database to WAL journal mode. No existing columns or data are
touched.

Idempotent: every statement uses ``CREATE INDEX IF NOT EXISTS`` and
``PRAGMA journal_mode=WAL`` is a no-op once the DB is already in WAL mode,
so running this repeatedly is safe.

Usage:
    python scripts/migrations/001_indexes.py [path/to/linkedin_jobs.db]

Default DB path is ``linkedin_jobs.db`` in the current working directory.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Allow running as a bare script (python scripts/migrations/001_indexes.py):
# add the repo root to sys.path so `scripts.create_db` imports.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Single source of truth for the index DDL — no local copy to keep in sync.
from scripts.create_db import INDEX_STATEMENTS  # noqa: E402

DEFAULT_DB_PATH = "linkedin_jobs.db"


def migrate(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Apply migration 001 to the SQLite database at ``db_path``."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(f"error: database not found: {db_path}")

    print(f"[001_indexes] target database: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Snapshot which indexes already exist so we can report what this run did.
        existing = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }

        for name, stmt in INDEX_STATEMENTS.items():
            cursor.execute(stmt)
            if name in existing:
                print(f"[001_indexes] index {name}: already present, skipped")
            else:
                print(f"[001_indexes] index {name}: created")
        conn.commit()

        # journal_mode must be set outside an open transaction (commit above).
        before = cursor.execute("PRAGMA journal_mode").fetchone()[0]
        after = cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if before.lower() == "wal":
            print("[001_indexes] journal_mode: already 'wal', unchanged")
        elif after.lower() == "wal":
            print(f"[001_indexes] journal_mode: '{before}' -> '{after}'")
        else:
            # Not fatal: the indexes (the substantive change) are already
            # committed. WAL commonly can't be enabled on network filesystems.
            print(
                f"[001_indexes] warning: WAL not enabled (still '{after}') — "
                "indexes applied successfully; WAL often fails on network filesystems"
            )
    finally:
        conn.close()

    print("[001_indexes] done.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate(path)
